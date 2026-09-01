"""Copied Google Places adapter from HungryRadar."""

from __future__ import annotations

from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse
import re

import httpx

from ..models import Place
from ..booking import classify_booking_provider

_BASE_URL = "https://places.googleapis.com/v1"
_SEARCH_FIELD_MASK = (
    "places.id,places.displayName,places.formattedAddress,"
    "places.rating,places.priceLevel"
)
_DETAILS_FIELD_MASK = (
    "id,displayName,formattedAddress,googleMapsUri,editorialSummary,"
    "websiteUri,rating,priceLevel,businessStatus,regularOpeningHours"
    ",reservable"
)
_LOCATION_DETAILS_FIELD_MASK = "id,displayName,formattedAddress,location,types"

_BOOKING_WORDS = re.compile(r"reserve|reservation|book[- ]?a[- ]?table|book[- ]?now|resy|opentable|tock", re.I)


class _BookingLinkParser(HTMLParser):
    """Find the first explicit reservation link on a restaurant website."""

    def __init__(self, base_url: str) -> None:
        super().__init__()
        self.base_url = base_url
        self.booking_url: str | None = None
        self._text = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.booking_url or tag.lower() not in {"a", "iframe"}:
            # OpenTable's widget commonly puts the provider URL on an
            # <input> submit control instead of an anchor. Handle that below.
            if tag.lower() != "input":
                return
        values = dict(attrs)
        href = values.get("href") or values.get("src") or ""
        embedded = values.get("data-ot-restref")
        if embedded:
            params = dict(parse_qsl(embedded, keep_blank_values=True))
            params.pop("rid", None)
            if params.get("restref"):
                self.booking_url = (
                    "https://www.opentable.com/booking/restref/availability?"
                    + urlencode(params)
                )
                return
        label = " ".join(filter(None, (values.get("aria-label"), values.get("title"), self._text)))
        if _BOOKING_WORDS.search(f"{label} {href}"):
            candidate = urljoin(self.base_url, href)
            if urlparse(candidate).scheme in {"http", "https"}:
                self.booking_url = candidate

    def handle_data(self, data: str) -> None:
        self._text = getattr(self, "_text", "") + f" {data}"

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a":
            self._text = ""


def _website_booking_link(website_uri: str | None) -> tuple[str | None, str | None]:
    """Resolve an explicit reservation link without guessing a provider URL."""
    if not website_uri:
        return None, None
    try:
        response = httpx.get(website_uri, timeout=4.0, follow_redirects=True)
        response.raise_for_status()
        parser = _BookingLinkParser(str(response.url))
        parser.feed(response.text[:1_000_000])
        if parser.booking_url:
            provider = classify_booking_provider(parser.booking_url, website_uri)
            return parser.booking_url, provider
    except Exception:
        pass
    return None, None


def search_places(api_key: str, query: str, max_results: int = 5) -> list[dict]:
    """Find candidate restaurants for a group recommendation."""
    checked_at = datetime.now(timezone.utc).isoformat()
    response = httpx.post(
        f"{_BASE_URL}/places:searchText",
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": api_key,
            "X-Goog-FieldMask": _SEARCH_FIELD_MASK,
        },
        json={"textQuery": query, "maxResultCount": max_results},
        timeout=10.0,
    )
    response.raise_for_status()
    return [
        {
            "place_id": place["id"],
            "name": place.get("displayName", {}).get("text", ""),
            "address": place.get("formattedAddress", ""),
            "rating": place.get("rating"),
            "price_level": place.get("priceLevel"),
            "source_uri": f"{_BASE_URL}/places:searchText",
            "checked_at": checked_at,
        }
        for place in response.json().get("places", [])
    ]


def autocomplete_locations(
    api_key: str,
    query: str,
    *,
    session_token: str | None = None,
    cities_only: bool = False,
    max_results: int = 5,
) -> list[dict]:
    """Return Google Places predictions for city or guest-origin selection."""
    body: dict[str, object] = {"input": query, "includeQueryPredictions": False}
    if cities_only:
        body["includedPrimaryTypes"] = ["(cities)"]
    if session_token:
        body["sessionToken"] = session_token
    response = httpx.post(
        f"{_BASE_URL}/places:autocomplete",
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": api_key,
            "X-Goog-FieldMask": "suggestions.placePrediction.placeId,suggestions.placePrediction.text,suggestions.placePrediction.structuredFormat",
        },
        json=body,
        timeout=10.0,
    )
    response.raise_for_status()
    predictions = []
    for suggestion in response.json().get("suggestions", [])[:max_results]:
        prediction = suggestion.get("placePrediction", {})
        if prediction.get("placeId"):
            predictions.append({
                "place_id": prediction["placeId"],
                "text": prediction.get("text", {}).get("text", ""),
                "main_text": prediction.get("structuredFormat", {}).get("mainText", {}).get("text", ""),
                "secondary_text": prediction.get("structuredFormat", {}).get("secondaryText", {}).get("text", ""),
            })
    return predictions


def get_location_details(api_key: str, place_id: str, *, session_token: str | None = None) -> dict:
    """Return canonical label and coordinates for a selected location."""
    response = httpx.get(
        f"{_BASE_URL}/places/{place_id}",
        headers={"X-Goog-Api-Key": api_key, "X-Goog-FieldMask": _LOCATION_DETAILS_FIELD_MASK},
        params={"sessionToken": session_token} if session_token else None,
        timeout=10.0,
    )
    response.raise_for_status()
    data = response.json()
    location = data.get("location") or {}
    return {
        "place_id": data.get("id", place_id),
        # Keep the user-facing/stored label coarse; do not persist a full
        # formatted street address for an optional guest origin.
        "label": data.get("displayName", {}).get("text", "") or data.get("formattedAddress", ""),
        "name": data.get("displayName", {}).get("text", ""),
        "latitude": location.get("latitude"),
        "longitude": location.get("longitude"),
        "types": data.get("types", []),
    }


def get_place(api_key: str, place_id: str) -> tuple[Place, dict]:
    """Hydrate a canonical place and supporting opening-hours evidence."""
    checked_at = datetime.now(timezone.utc).isoformat()
    response = httpx.get(
        f"{_BASE_URL}/places/{place_id}",
        headers={"X-Goog-Api-Key": api_key, "X-Goog-FieldMask": _DETAILS_FIELD_MASK},
        timeout=10.0,
    )
    response.raise_for_status()
    data = response.json()
    website_uri = data.get("websiteUri")
    booking_uri, booking_provider = _website_booking_link(website_uri)
    place = Place(
        place_id=data["id"],
        name=data.get("displayName", {}).get("text", ""),
        address=data.get("formattedAddress", ""),
        google_maps_uri=data.get("googleMapsUri"),
        description=data.get("editorialSummary", {}).get("text"),
        website_uri=website_uri,
        booking_uri=booking_uri,
        booking_provider=booking_provider,
        rating=data.get("rating"),
        price_level=data.get("priceLevel"),
        reservable=data.get("reservable"),
    )
    return place, {
        "business_status": data.get("businessStatus"),
        "regular_opening_hours": data.get("regularOpeningHours", {}).get(
            "weekdayDescriptions"
        ),
        "source_uri": place.google_maps_uri or f"{_BASE_URL}/places/{place_id}",
        "checked_at": checked_at,
    }
