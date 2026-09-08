from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from groupreservations import database
from groupreservations.evidence import get_survey_evidence


def test_concurrent_guest_submissions_are_independent(tmp_path):
    object.__setattr__(database.settings, "database_path", str(tmp_path / "test.sqlite3"))
    organizer = database.create_user("organizer@example.com", "cognito-sub-1")
    survey = database.create_survey(
        organizer["id"], "Dinner", "San Francisco", ["2026-09-04"], ["19:00"],
        {"cuisine": ["Italian", "Japanese"]},
    )

    def submit(index):
        return database.append_response(
            survey["public_token"], f"guest-token-{index}",
            ["2026-09-04"], ["19:00"], {"cuisine": ["Italian" if index % 2 else "Japanese"]},
        )

    with ThreadPoolExecutor(max_workers=3) as pool:
        responses = list(pool.map(submit, range(3)))

    stored = database.aggregate_survey(survey["id"])
    assert len({response["respondent_user_id"] for response in responses}) == 3
    assert stored["response_count"] == 3
    assert stored["preference_summary"]["cuisine"] == {"Japanese": 2, "Italian": 1}


def test_same_guest_updates_one_response(tmp_path):
    object.__setattr__(database.settings, "database_path", str(tmp_path / "test.sqlite3"))
    organizer = database.create_user("organizer@example.com", "cognito-sub-2")
    survey = database.create_survey(
        organizer["id"], "Dinner", "San Francisco", ["2026-09-04"], ["19:00"],
        {"cuisine": ["Italian", "Japanese"]},
    )
    first = database.append_response(survey["public_token"], "same-guest-token", ["2026-09-04"], ["19:00"], {"cuisine": ["Italian"]})
    second = database.append_response(survey["public_token"], "same-guest-token", ["2026-09-04"], ["19:00"], {"cuisine": ["Japanese"]})
    stored = database.aggregate_survey(survey["id"])
    assert first["respondent_user_id"] == second["respondent_user_id"]
    assert stored["response_count"] == 1
    assert stored["preference_summary"]["cuisine"] == {"Japanese": 1}


def test_guest_origin_is_persisted_for_private_recommendation_context(tmp_path):
    object.__setattr__(database.settings, "database_path", str(tmp_path / "test.sqlite3"))
    organizer = database.create_user("origin-organizer@example.com", "cognito-origin")
    survey = database.create_survey(
        organizer["id"], "Dinner", "San Francisco", ["2026-09-04"], ["19:00"],
        {"distance": ["5", "10"]},
    )
    database.append_response(
        survey["public_token"], "origin-guest-token", ["2026-09-04"], ["19:00"],
        {"distance": ["10"]}, "place-123", "Mission District", 37.7599, -122.4148,
    )
    stored = database.aggregate_survey(survey["id"])
    response = stored["report"]["responses"][0]
    assert response["origin_place_id"] == "place-123"
    assert response["origin_label"] == "Mission District"
    assert response["origin_lat"] == 37.7599
    assert stored["preference_summary"]["distance"] == {"10": 1}


def test_created_survey_is_publicly_readable_with_normalized_options(tmp_path):
    object.__setattr__(database.settings, "database_path", str(tmp_path / "test.sqlite3"))
    organizer = database.create_user("organizer@example.com", "cognito-sub-3")
    survey = database.create_survey(
        organizer["id"], "Dinner", "San Clemente", ["2026-09-04"], ["19:00"],
        {"cuisine": ["Italian", "Japanese"], "price": ["$", "$$"]},
    )
    public = database.get_survey(survey["public_token"])
    assert public["id"] == survey["id"]
    assert public["questions"] == {"cuisine": ["Italian", "Japanese"], "price": ["$", "$$"]}


def test_disabled_or_unknown_options_are_excluded_from_saved_answers(tmp_path):
    object.__setattr__(database.settings, "database_path", str(tmp_path / "test.sqlite3"))
    organizer = database.create_user("organizer@example.com", "cognito-sub-4")
    survey = database.create_survey(
        organizer["id"], "Dinner", "San Clemente", ["2026-09-04"], ["19:00"],
        {"cuisine": ["Italian", "Japanese"]},
    )
    database.append_response(
        survey["public_token"], "guest-filter-test", ["2026-09-04"], ["19:00"],
        {"cuisine": ["Italian", "Mexican"]},
    )
    stored = database.aggregate_survey(survey["id"])
    assert stored["preference_summary"]["cuisine"] == {"Italian": 1}


def test_aggregate_exposes_all_tied_date_time_pairs(tmp_path):
    object.__setattr__(database.settings, "database_path", str(tmp_path / "test.sqlite3"))
    organizer = database.create_user("organizer@example.com", "cognito-sub-5")
    survey = database.create_survey(
        organizer["id"], "Dinner", "San Clemente",
        ["2026-09-04", "2026-09-11"], ["18:00", "19:00"], {},
    )
    database.append_response(survey["public_token"], "guest-pair-a", ["2026-09-04"], ["18:00"], {})
    database.append_response(survey["public_token"], "guest-pair-b", ["2026-09-11"], ["19:00"], {})
    pairs = database.aggregate_survey(survey["id"])["report"]["schedule"]["pair_leaders"]
    assert {(pair["date"], pair["time"]) for pair in pairs} == {
        ("2026-09-04", "18:00"), ("2026-09-11", "19:00")
    }


def test_survey_defaults_to_a_two_day_response_window(tmp_path):
    object.__setattr__(database.settings, "database_path", str(tmp_path / "test.sqlite3"))
    organizer = database.create_user("expiry-organizer@example.com", "cognito-expiry-0")
    survey = database.create_survey(
        organizer["id"], "Dinner", "San Clemente", ["2026-09-04"], ["19:00"], {},
    )
    window = datetime.fromisoformat(survey["expires_at"]) - datetime.fromisoformat(survey["created_at"])
    assert timedelta(days=1, hours=23) <= window <= timedelta(days=2, minutes=1)
    assert survey["is_open"] is True


def test_responses_after_expiry_are_rejected(tmp_path):
    object.__setattr__(database.settings, "database_path", str(tmp_path / "test.sqlite3"))
    organizer = database.create_user("expiry-organizer@example.com", "cognito-expiry-1")
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    survey = database.create_survey(
        organizer["id"], "Dinner", "San Clemente", ["2026-09-04"], ["19:00"],
        {"cuisine": ["Italian", "Japanese"]}, expires_at=past,
    )
    assert database.get_survey(survey["public_token"])["is_open"] is False
    with pytest.raises(database.SurveyClosed):
        database.append_response(
            survey["public_token"], "late-guest-token", ["2026-09-04"], ["19:00"],
            {"cuisine": ["Italian"]},
        )
    assert database.aggregate_survey(survey["id"])["response_count"] == 0


def test_responses_before_expiry_are_accepted(tmp_path):
    object.__setattr__(database.settings, "database_path", str(tmp_path / "test.sqlite3"))
    organizer = database.create_user("expiry-organizer@example.com", "cognito-expiry-2")
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    survey = database.create_survey(
        organizer["id"], "Dinner", "San Clemente", ["2026-09-04"], ["19:00"],
        {"cuisine": ["Italian", "Japanese"]}, expires_at=future,
    )
    assert database.get_survey(survey["public_token"])["is_open"] is True
    database.append_response(
        survey["public_token"], "prompt-guest-token", ["2026-09-04"], ["19:00"],
        {"cuisine": ["Japanese"]},
    )
    assert database.aggregate_survey(survey["id"])["response_count"] == 1


def test_date_specific_time_slots_do_not_create_invalid_pairs(tmp_path):
    object.__setattr__(database.settings, "database_path", str(tmp_path / "test.sqlite3"))
    organizer = database.create_user("schedule-organizer@example.com", "cognito-schedule")
    survey = database.create_survey(
        organizer["id"], "Dinner", "San Clemente", ["2026-09-04", "2026-09-11"], ["18:00", "20:00"], {},
        availability={"2026-09-04": ["18:00"], "2026-09-11": ["20:00"]},
    )
    database.append_response(
        survey["public_token"], "schedule-guest-token", ["2026-09-04", "2026-09-11"], ["18:00", "20:00"], {},
        availability={"2026-09-04": ["18:00"], "2026-09-11": ["20:00"]},
    )
    stored = database.aggregate_survey(survey["id"])
    pairs = {(pair["date"], pair["time"]) for pair in stored["report"]["schedule"]["pair_leaders"]}
    assert pairs == {("2026-09-04", "18:00"), ("2026-09-11", "20:00")}


def test_agent_evidence_tool_returns_summary_only_to_organizer(tmp_path):
    object.__setattr__(database.settings, "database_path", str(tmp_path / "test.sqlite3"))
    organizer = database.create_user("evidence@example.com", "cognito-evidence")
    survey = database.create_survey(
        organizer["id"], "Dinner", "San Clemente", ["2026-09-04"], ["19:00"],
        {"cuisine": ["Italian"]},
    )
    database.append_response(
        survey["public_token"], "evidence-guest-token", ["2026-09-04"], ["19:00"],
        {"cuisine": ["Italian"]}, origin_label="Private address", origin_lat=1, origin_lng=2,
    )

    evidence = get_survey_evidence(survey["id"], organizer["id"])
    assert evidence["success"] is True
    assert evidence["schedule"]["times_by_date"] == {"2026-09-04": ["19:00"]}
    assert evidence["preferences"]["cuisine"]["votes"] == {"Italian": 1}
    assert "responses" not in evidence
    assert get_survey_evidence(survey["id"], "another-organizer")["success"] is False
