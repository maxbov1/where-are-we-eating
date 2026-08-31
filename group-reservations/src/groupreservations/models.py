"""Copied and product-neutralized restaurant evidence models from HungryRadar."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Place:
    """Canonical restaurant identity hydrated from Google Places."""

    place_id: str
    name: str
    address: str
    google_maps_uri: str | None = None
    google_reservation_uri: str | None = None
    description: str | None = None
    website_uri: str | None = None
    booking_uri: str | None = None
    booking_provider: str | None = None
    opentable_uri: str | None = None
    menu_uri: str | None = None
    rating: float | None = None
    price_level: str | None = None
    reservable: bool | None = None
    timezone: str | None = None
    directions_uri: str | None = None


@dataclass(frozen=True)
class AvailabilityEvidence:
    """Evidence for one restaurant/date/time slot."""

    available: bool = False
    waitlist_available: bool = False
    source_uri: str | None = None
    checked_at: str | None = None
