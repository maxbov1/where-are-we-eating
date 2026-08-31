import pytest

from groupreservations.booking import (
    build_booking_handoff,
    build_opentable_availability_url,
    classify_booking_provider,
)
from groupreservations.adapters.google_places import _BookingLinkParser
from groupreservations.models import Place


def test_opentable_handoff_prefills_booking_details():
    place = Place(
        place_id="p1", name="Buona Forchetta", address="San Clemente",
        booking_uri="https://www.opentable.com/r/buona-forchetta-san-clemente",
        booking_provider="OpenTable",
    )
    handoff = build_booking_handoff(place, date="2026-09-11", time="19:00", party_size=4)
    assert handoff is not None
    assert handoff.prefilled is True
    assert handoff.availability_verified is False
    assert "dateTime=2026-09-11T19%3A00%3A00" in handoff.url
    assert "covers=4" in handoff.url


def test_google_reserve_handoff_has_priority_over_restaurant_booking_page():
    place = Place(
        place_id="p-google", name="Pronto Cucina", address="San Clemente",
        google_maps_uri="https://maps.google.com/?cid=123",
        google_reservation_uri="https://www.google.com/maps/reserve/v/dine/c/token123",
        booking_uri="https://restaurant.example/reservations",
        booking_provider="Restaurant website",
    )
    handoff = build_booking_handoff(place, date="2026-09-04", time="20:00", party_size=4)
    assert handoff is not None
    assert handoff.provider == "Google Reserve"
    assert handoff.url == "https://www.google.com/maps/reserve/v/dine/c/token123"
    assert handoff.prefilled is False


def test_opentable_restref_url_preserves_provider_context_and_merges_params():
    source = (
        "https://www.opentable.com/booking/restref/availability?restref=1330258"
        "&lang=en-US&ot_source=Restaurant%20website&corrid=abc123"
    )
    url = build_opentable_availability_url(
        source, date="2026-09-04", time="20:00", party_size=4
    )
    assert "restref=1330258" in url
    assert "lang=en-US" in url
    assert "ot_source=Restaurant+website" in url
    assert "corrid=abc123" in url
    assert "dateTime=2026-09-04T20%3A00%3A00" in url
    assert "covers=4" in url
    assert url.count("?") == 1


def test_opentable_restref_client_anchor_is_normalized_without_following_it():
    source = (
        "https://www.opentable.com/restref/client/?restref=1330258"
        "&lang=en-US&ot_source=Restaurant%20website&corrid=abc123"
    )
    url = build_opentable_availability_url(
        source, date="2026-09-04", time="20:00", party_size=4
    )
    assert url.startswith("https://www.opentable.com/booking/restref/availability?")
    assert "restref=1330258" in url
    assert "dateTime=2026-09-04T20%3A00%3A00" in url
    assert "covers=4" in url
    assert url.count("?") == 1


def test_opentable_widget_input_exposes_verified_restref_without_navigation():
    parser = _BookingLinkParser("https://www.parlorsanclemente.com/reservations")
    parser.feed(
        '<input type="submit" data-ot-restref="rid=1379182&amp;restref=1379182&amp;'
        'partysize=2&amp;datetime=2026-08-31T00%3A00%3A00&amp;lang=en-US" '
        'data-ot-path="/booking/restref/availability" value="Find a Table">'
    )
    assert parser.booking_url is not None
    assert "restref=1379182" in parser.booking_url
    assert parser.booking_url.startswith(
        "https://www.opentable.com/booking/restref/availability?"
    )


def test_opentable_id_can_build_current_availability_path():
    url = build_opentable_availability_url(1330258)
    assert url == "https://www.opentable.com/booking/restref/availability?restref=1330258"


def test_opentable_url_builder_rejects_unverified_names_and_other_sites():
    with pytest.raises(ValueError):
        build_opentable_availability_url("Buona Forchetta San Clemente")
    with pytest.raises(ValueError):
        build_opentable_availability_url("https://restaurant.example/reservations")


def test_custom_website_fallback_preserves_exact_url():
    place = Place(
        place_id="p2", name="Local Table", address="San Clemente",
        booking_uri="https://local.example/reservations",
        booking_provider="Restaurant website",
    )
    handoff = build_booking_handoff(place, date="2026-09-11", time="19:00", party_size=4)
    assert handoff is not None
    assert handoff.url == "https://local.example/reservations"
    assert handoff.prefilled is False


def test_unknown_provider_without_booking_url_returns_no_handoff():
    place = Place(place_id="p3", name="No Link", address="San Clemente")
    assert build_booking_handoff(place, date="2026-09-11", time="19:00", party_size=4) is None


def test_booking_provider_is_labeled_from_known_external_host():
    assert classify_booking_provider("https://resy.com/cities/sf/venues/local") == "Resy"


def test_booking_provider_labels_same_site_as_restaurant_website():
    assert classify_booking_provider(
        "https://restaurant.example/book", "https://restaurant.example/menu"
    ) == "Restaurant website"
