import json
import threading
from pathlib import Path

import pytest

from groupreservations.booking import (
    build_booking_handoff,
    build_opentable_availability_url,
    classify_booking_provider,
)
from groupreservations.agent_state import AgentState
from groupreservations.reservation_browser import (
    ReservationBrowser,
    _candidate_id,
    _candidate_score,
    _is_skip_control,
    _matches_location,
    _provider_identifiers,
    _reservation_signal,
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


def test_opentable_restref_anchor_replaces_stale_prefill_aliases():
    url = build_opentable_availability_url(
        "https://www.opentable.com/booking/restref/availability?rid=255298&"
        "restRef=255298&partySize=2&dateTime=2026-06-24T19%3A00",
        date="2026-09-11", time="19:00", party_size=5,
    )
    assert "restref=255298" in url
    assert "dateTime=2026-09-11T19%3A00%3A00" in url
    assert "covers=5" in url
    assert "partySize=2" not in url
    assert "2026-06-24" not in url


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
    assert classify_booking_provider(
        "https://tables.toasttab.com/restaurants/venue-7/findTime"
    ) == "Toast"


def test_booking_provider_labels_same_site_as_restaurant_website():
    assert classify_booking_provider(
        "https://restaurant.example/book", "https://restaurant.example/menu"
    ) == "Restaurant website"


def test_reservation_dom_scoring_is_provider_neutral():
    score, confidence = _candidate_score(
        {
            "href": "https://unknown-widget.example/dine/venue-123",
            "label": "Reserve a table",
            "nearby_text": "Reservations",
            "attributes": {"data-venue-id": "venue-123"},
            "iframe": True,
        },
        "https://restaurant.example",
    )
    assert score >= 8
    assert confidence == "high"


def test_reservation_dom_extracts_generic_provider_identifier_attributes():
    identifiers = _provider_identifiers(
        {"attributes": {"data-provider-id": "abc", "class": "reservation-widget"}}
    )
    assert identifiers == [{"attribute": "data-provider-id", "value": "abc"}]


def test_reservation_dom_recognizes_phone_number_near_reservation_language():
    score, confidence = _candidate_score(
        {
            "href": "",
            "label": "Call for reservations: (415) 555-1212",
            "nearby_text": "Reservations",
            "attributes": {},
            "iframe": False,
        },
        "https://restaurant.example",
    )
    assert score >= 12
    assert confidence == "high"


def test_reservation_location_matching_uses_dom_container_context():
    candidate = {
        "label": "RESERVATIONS",
        "container_text": "SAN CLEMENTE 1014 S El Camino Real RESERVATIONS",
        "href": "https://www.opentable.com/restref/client/?restref=1330258",
    }
    assert _matches_location(candidate, "San Clemente, CA") is True
    assert _matches_location(candidate, "Coronado, CA") is False


def test_browser_candidate_identity_is_stable_and_url_bound():
    first = _candidate_id("https://restaurant.example/reservations", "Reserve")
    second = _candidate_id("https://restaurant.example/reservations", "Reserve")
    different_url = _candidate_id("https://other.example/reservations", "Reserve")
    assert first == second
    assert first != different_url


def test_successful_browser_responses_do_not_repeat_full_agent_state():
    browser = _fake_browser()

    result = json.loads(browser._response(
        {"success": True, "url": browser.page.url},
        phase="reservation_scan",
    ))

    assert "agent_state" not in result
    assert result["candidate_id"] == _candidate_id(browser.page.url)


def test_failed_browser_responses_keep_state_for_recovery():
    browser = _fake_browser()

    result = json.loads(browser._response(
        {"success": False, "url": browser.page.url, "error": "blocked"},
        phase="reservation_scan",
    ))

    assert result["agent_state"]["last_error"] == "blocked"


class _FakeResponse:
    status = 200


class _FakeLocator:
    def __init__(self, candidates):
        self.candidates = candidates

    def evaluate_all(self, _script):
        return self.candidates

    def inner_text(self):
        return "Find a table"

    def aria_snapshot(self, **_kwargs):
        return '- button "Find a table"'


class _FakeFrame:
    def __init__(self, url):
        self.url = url


class _FakePage:
    def __init__(self):
        self.url = "https://restaurant.example/reservations"
        self.main_frame = self
        self.frames = [_FakeFrame("https://tables.toasttab.com/restaurants/venue-7/findTime")]
        self.candidates = [{
            "tag": "iframe",
            "href": "https://tables.toasttab.com/restaurants/venue-7/findTime",
            "tel": None,
            "label": "Toast Reservation",
            "nearby_text": "",
            "container_text": "",
            "attributes": {"title": "Toast Reservation"},
            "iframe": True,
        }, {
            "tag": "a",
            "href": "https://restaurant.example/menu",
            "tel": None,
            "label": "Menu",
            "nearby_text": "",
            "container_text": "",
            "attributes": {},
            "iframe": False,
        }, {
            "tag": "script",
            "href": "https://cdn.example/widget.js",
            "tel": None,
            "label": "",
            "nearby_text": "Reservations",
            "container_text": "Reservations",
            "attributes": {},
            "iframe": False,
        }]

    def goto(self, url, **_kwargs):
        self.url = url
        return _FakeResponse()

    def title(self):
        return "Restaurant reservations"

    def evaluate(self, _script):
        return None

    def wait_for_timeout(self, _milliseconds):
        return None

    def screenshot(self, path, **_kwargs):
        Path(path).write_bytes(b"fake-png")

    def locator(self, _selector):
        return _FakeLocator(self.candidates)


def _fake_browser():
    browser = ReservationBrowser.__new__(ReservationBrowser)
    # Keep this lightweight fixture aligned with ReservationBrowser.__init__.
    # The browser tests run on the owner thread, so no Playwright runtime or
    # executor is needed, but the isolated workflow maps must exist.
    browser.playwright = None
    browser.browser = None
    browser.page = _FakePage()
    browser.candidate_pages = {}
    browser.workflow_pages = {}
    browser.state = AgentState()
    browser.profile_dir = Path(".test-reservation-browser")
    browser.owner_thread_id = threading.get_ident()
    return browser


def test_browser_observe_returns_dom_and_accessibility_evidence(tmp_path):
    browser = _fake_browser()
    browser.profile_dir = tmp_path
    scan = json.loads(browser.scan_dom(browser.page.url))
    verified = json.loads(browser.verify(scan["candidate_id"], scan["url"]))

    observed = json.loads(browser.observe(verified["candidate_id"], verified["url"]))

    assert observed["success"] is True
    assert observed["dom"]["text"] == "Find a table"
    assert observed["accessibility_snapshot"] == '- button "Find a table"'
    assert observed["screenshot_path"]
    assert Path(observed["screenshot_path"]).exists()


def test_browser_scan_and_verification_are_candidate_bound():
    browser = _fake_browser()

    scan = json.loads(browser.scan_dom("https://restaurant.example/reservations"))

    assert scan["success"] is True
    assert scan["candidate"]["url"] == "https://restaurant.example/reservations"
    assert scan["candidate_id"] == _candidate_id(scan["candidate"]["url"])
    assert scan["candidates"][0]["candidate_id"]
    assert scan["candidates"][0]["tag"] == "iframe"
    assert scan["candidates"][0]["url"].startswith("https://tables.toasttab.com/")
    assert scan["next_action"]["tool"] == "reservation_verify"
    assert scan["next_action"]["url"] == scan["candidates"][0]["url"]
    assert not any(item["tag"] == "script" for item in scan["candidates"])
    assert scan["candidates"][0]["source_url"] == scan["url"]
    assert {action["tool"] for action in scan["available_actions"]} >= {
        "reservation_verify", "reservation_inspect"
    }

    iframe = scan["candidates"][0]
    verified = json.loads(browser.verify(iframe["candidate_id"], iframe["url"]))

    assert verified["success"] is True
    assert verified["verified"] is True
    assert browser.state.verification["verified"] is True

    prepared = json.loads(browser.prepare(
        iframe["candidate_id"], iframe["url"], iframe["url"],
        "2026-09-11", "19:00", 5,
    ))
    assert prepared["success"] is True
    assert prepared["provider"] == "Toast"
    assert prepared["booking_url"] == scan["candidates"][0]["url"]


def test_workflow_handle_resolves_to_surface_and_errors_are_recoverable():
    browser = _fake_browser()
    surface_id = "surface-1"
    workflow_id = "workflow-1"
    browser.state.workflows[workflow_id] = {
        "workflow_id": workflow_id,
        "surface_id": surface_id,
    }
    browser.state.scanned_candidates[surface_id] = {
        "candidate_id": surface_id,
        "url": browser.page.url,
        "source_url": browser.page.url,
        "workflow_id": workflow_id,
    }
    browser.state.verifications[surface_id] = {
        "candidate_id": surface_id,
        "url": browser.page.url,
        "source_url": browser.page.url,
        "verified": True,
    }

    assert browser._surface_id(workflow_id) == surface_id
    assert browser._workflow_id(workflow_id) == workflow_id
    assert browser._verified_target(workflow_id, browser.page.url) is browser.page

    error = browser._structured_error(
        workflow_id, browser.page.url, "ACTION_URL_NOT_AUTHORIZED",
        "Booking URL was not observed in the selected surface.",
        "reservation_expand", "Expand the selected surface and inspect action_urls.",
    )
    assert error["status"] == "blocked"
    assert error["workflow_id"] == workflow_id
    assert error["surface_id"] == surface_id
    assert error["recovery"]["tool"] == "reservation_expand"


def test_skip_and_unrelated_surface_controls_cannot_authorize_booking_urls():
    skip = {"tag": "a", "label": "Skip to Main Content", "href": "https://www.opentable.com/r/wrong"}
    unrelated = {"tag": "a", "label": "About Us", "href": "https://www.opentable.com/r/wrong"}
    reservation = {"tag": "a", "label": "Reserve a Table", "href": "https://www.opentable.com/r/right"}

    assert _is_skip_control(skip) is True
    assert _candidate_score(skip, "https://restaurant.example/")[1] == "excluded"
    assert _reservation_signal(unrelated) is False
    assert _reservation_signal(reservation) is True


def test_browser_verifies_and_targets_an_embedded_frame_without_navigation():
    browser = _fake_browser()
    iframe_url = browser.page.candidates[0]["href"]
    frame = _FakeFrame(iframe_url)
    browser.page.frames = [frame]
    scan = json.loads(browser.scan_dom(browser.page.url))
    iframe = scan["candidates"][0]

    verified = json.loads(browser.verify(iframe["candidate_id"], iframe["url"]))

    assert verified["success"] is True
    assert browser._verified_target(iframe["candidate_id"], iframe["url"]) is frame


def test_browser_prepare_rejects_unobserved_booking_url():
    browser = _fake_browser()
    scan = json.loads(browser.scan_dom("https://restaurant.example/reservations"))
    verified = json.loads(browser.verify(scan["candidate_id"], scan["url"]))
    assert verified["success"] is True

    prepared = json.loads(browser.prepare(
        scan["candidate_id"], scan["url"], "https://tables.toasttab.com/restaurants/other/findTime",
        "2026-09-11", "19:00", 5,
    ))
    assert prepared["success"] is False
    assert "not observed" in prepared["error"]


def test_browser_verification_mismatch_enters_failure_transition():
    browser = _fake_browser()
    scan = json.loads(browser.scan_dom("https://restaurant.example/reservations"))

    failed = json.loads(browser.verify(scan["candidate_id"], "https://other.example/reservations"))

    assert failed["success"] is False
    assert failed["verified"] is False
    assert failed["available_actions"][0]["tool"] == "reservation_open"
    assert browser.state.phase == "reservation_verification_failure"
    assert browser.state.status == "failed"
    assert browser.state.last_error


def test_browser_interactions_reject_unverified_candidate_state():
    browser = _fake_browser()
    candidate_id = _candidate_id(browser.page.url)

    fill_result = json.loads(browser.fill(candidate_id, browser.page.url, "date", "2026-09-11"))
    click_result = json.loads(browser.click(candidate_id, browser.page.url, "Find a table"))

    assert fill_result["success"] is False
    assert click_result["success"] is False
    assert {"reservation_open", "reservation_sweep"}.issubset(
        {item["tool"] for item in fill_result["available_actions"]}
    )
    assert browser.state.phase == "reservation_precondition_failure"
