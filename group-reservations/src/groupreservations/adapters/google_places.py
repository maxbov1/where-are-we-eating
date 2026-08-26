"""Copied Google Places adapter from HungryRadar."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx

from ..models import Place

_BASE_URL = "https://places.googleapis.com/v1"
_SEARCH_FIELD_MASK = (
    "places.id,places.displayName,places.formattedAddress,"
    "places.rating,places.priceLevel"
)
_DETAILS_FIELD_MASK = (
    "id,displayName,formattedAddress,googleMapsUri,editorialSummary,"
    "websiteUri,rating,priceLevel,businessStatus,regularOpeningHours"
)


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
    place = Place(
        place_id=data["id"],
        name=data.get("displayName", {}).get("text", ""),
        address=data.get("formattedAddress", ""),
        google_maps_uri=data.get("googleMapsUri"),
        description=data.get("editorialSummary", {}).get("text"),
        website_uri=data.get("websiteUri"),
        rating=data.get("rating"),
        price_level=data.get("priceLevel"),
    )
    return place, {
        "business_status": data.get("businessStatus"),
        "regular_opening_hours": data.get("regularOpeningHours", {}).get(
            "weekdayDescriptions"
        ),
        "source_uri": place.google_maps_uri or f"{_BASE_URL}/places/{place_id}",
        "checked_at": checked_at,
    }
