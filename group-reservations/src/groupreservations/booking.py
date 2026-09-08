"""Small, provider-aware booking handoff helpers."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from strands import tool

from .models import Place

logger = logging.getLogger(__name__)

KNOWN_PROVIDERS = {
    "opentable.com": "OpenTable",
    "resy.com": "Resy",
    "exploretock.com": "Tock",
    "tockhq.com": "Tock",
    "toasttab.com": "Toast",
}


@dataclass(frozen=True)
class BookingHandoff:
    """A human-confirmed path to the provider's booking page."""

    url: str
    provider: str
    restaurant_name: str
    date: str
    time: str
    party_size: int
    prefilled: bool
    availability_verified: bool
    human_confirmation_required: bool = True


def classify_booking_provider(url: str, website_url: str | None = None) -> str:
    """Label a booking URL without claiming that its provider is supported."""
    hostname = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    if hostname == "google.com" and urlsplit(url).path.startswith("/maps/reserve/"):
        return "Google Reserve"
    for domain, provider in KNOWN_PROVIDERS.items():
        if hostname == domain or hostname.endswith(f".{domain}"):
            return provider
    site_host = (urlsplit(website_url or "").hostname or "").lower().removeprefix("www.")
    if site_host and (hostname == site_host or hostname.endswith(f".{site_host}")):
        return "Restaurant website"
    return f"Unknown provider ({hostname})" if hostname else "Unknown provider"


def _with_opentable_params(url: str, date: str, time: str, party_size: int) -> str:
    parts = urlsplit(url)
    params = dict(parse_qsl(parts.query, keep_blank_values=True))
    params.update({"dateTime": f"{date}T{time}:00", "covers": str(party_size)})
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(params), parts.fragment))


def build_opentable_availability_url(
    restaurant: str | int,
    *,
    date: str | None = None,
    time: str | None = None,
    party_size: int | None = None,
) -> str:
    """Build the current OpenTable availability path from verified input.

    ``restaurant`` must be either an exact OpenTable URL (preferably the
    ``/booking/restref/availability`` URL returned by a restaurant website) or
    a restaurant ID returned by OpenTable.  A restaurant name or slug is not a
    valid input: turning one into an ID would create a false booking path.
    """
    if isinstance(restaurant, int) or (isinstance(restaurant, str) and restaurant.isdigit()):
        url = f"https://www.opentable.com/booking/restref/availability?restref={restaurant}"
    elif isinstance(restaurant, str) and urlsplit(restaurant).hostname:
        host = (urlsplit(restaurant).hostname or "").lower().removeprefix("www.")
        if host != "opentable.com" and not host.endswith(".opentable.com"):
            raise ValueError("restaurant URL must be an OpenTable URL")
        url = restaurant
    else:
        raise ValueError("restaurant must be a verified OpenTable ID or URL")

    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    # Restaurant websites commonly link to /restref/client. That anchor is
    # enough to identify the venue; no redirect or OpenTable page load is
    # needed. Keep its baked-in provider context while using the availability
    # route that accepts date/time parameters.
    if parts.path.rstrip("/") == "/restref/client" and query.get("restref"):
        url = urlunsplit(
            (parts.scheme, parts.netloc, "/booking/restref/availability", parts.query, parts.fragment)
        )

    if date is None or time is None or party_size is None:
        return url
    return _with_opentable_params(url, date, time, party_size)


def build_booking_handoff(
    place: Place,
    *,
    date: str,
    time: str,
    party_size: int,
    availability_verified: bool = False,
) -> BookingHandoff | None:
    """Build a prefilled OpenTable handoff or exact custom booking fallback."""
    url = place.google_reservation_uri or place.booking_uri or place.opentable_uri
    if not url:
        return None
    provider = (
        "Google Reserve"
        if place.google_reservation_uri
        else place.booking_provider or classify_booking_provider(url, place.website_uri)
    )
    is_opentable = provider.casefold() == "opentable" or "opentable." in urlsplit(url).netloc.casefold()
    if is_opentable:
        url = build_opentable_availability_url(
            url, date=date, time=time, party_size=party_size
        )
    return BookingHandoff(
        url=url,
        provider=provider,
        restaurant_name=place.name,
        date=date,
        time=time,
        party_size=party_size,
        prefilled=is_opentable,
        availability_verified=availability_verified,
    )


@tool
def prepare_booking_link(url: str, date: str, time: str, party_size: int) -> str:
    """Prepare an exact provider booking URL for the requested group details."""
    provider = classify_booking_provider(url)
    logger.info("booking stage=prepare provider=%s date=%s time=%s party_size=%d", provider, date, time, party_size)
    if provider == "OpenTable":
        prepared = build_opentable_availability_url(
            url, date=date, time=time, party_size=party_size
        )
        logger.info("booking stage=prepared provider=OpenTable has_date_time=%s has_party_size=%s",
                    "dateTime=" in prepared, "covers=" in prepared)
        return prepared
    logger.info("booking stage=unchanged provider=%s", provider)
    return url
