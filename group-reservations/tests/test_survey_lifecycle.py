"""Network-free coverage for survey expiration, revocation, and response export."""

from datetime import datetime, timedelta, timezone

import pytest

from groupreservations import database


def _use_temp_database(tmp_path):
    object.__setattr__(database.settings, "database_path", str(tmp_path / "lifecycle.sqlite3"))


def _iso(**delta):
    return (datetime.now(timezone.utc) + timedelta(**delta)).isoformat()


def _day(**delta):
    return (datetime.now(timezone.utc) + timedelta(**delta)).date().isoformat()


def _survey(email="organizer@example.com", **overrides):
    organizer = database.create_user(email, None)
    defaults = {"dates": [_day(days=7)], "times": ["19:00"], "questions": {"cuisine": ["Italian", "Japanese"]}}
    return database.create_survey(organizer["id"], "Dinner", "San Clemente", **{**defaults, **overrides})


def test_new_survey_expires_a_grace_day_after_the_last_candidate_date(tmp_path):
    _use_temp_database(tmp_path)
    survey = _survey(dates=[_day(days=3), _day(days=10)])
    expires = database._parse_timestamp(survey["expires_at"])
    last_candidate_day = datetime.fromisoformat(f"{_day(days=10)}T00:00:00+00:00")
    assert survey["status"] == "active"
    # Voting survives the whole last candidate day in every timezone, plus a grace day.
    assert expires == last_candidate_day + timedelta(days=1 + database._EXPIRY_GRACE_DAYS)


def test_survey_whose_dates_have_passed_is_born_without_an_expiry(tmp_path):
    _use_temp_database(tmp_path)
    survey = _survey(dates=[_day(days=-30)])
    # Born expired would be nonsense; the organizer sets an explicit expiry instead.
    assert survey["expires_at"] is None
    assert survey["status"] == "active"


def test_explicit_expiry_at_creation_overrides_the_candidate_dates(tmp_path):
    _use_temp_database(tmp_path)
    chosen = _iso(days=1)
    assert _survey(dates=[_day(days=30)], expires_at=chosen)["expires_at"] == chosen


def test_status_becomes_expired_once_the_expiry_passes(tmp_path):
    _use_temp_database(tmp_path)
    survey = _survey(expires_at=_iso(seconds=-1))
    assert database.get_survey(survey["id"])["status"] == "expired"


def test_revocation_outranks_expiry_in_the_derived_status(tmp_path):
    _use_temp_database(tmp_path)
    survey = _survey(expires_at=_iso(seconds=-1))
    assert database.set_survey_revoked(survey["id"], True)["status"] == "revoked"


def test_expired_survey_rejects_new_responses(tmp_path):
    _use_temp_database(tmp_path)
    survey = _survey(expires_at=_iso(seconds=-1))
    with pytest.raises(database.SurveyClosed) as caught:
        database.append_response(survey["public_token"], "guest-1", [_day(days=7)], ["19:00"], {})
    assert caught.value.status == "expired"


def test_revoked_survey_rejects_new_responses(tmp_path):
    _use_temp_database(tmp_path)
    survey = _survey()
    database.set_survey_revoked(survey["id"], True)
    with pytest.raises(database.SurveyClosed) as caught:
        database.append_response(survey["public_token"], "guest-1", [_day(days=7)], ["19:00"], {})
    assert caught.value.status == "revoked"


def test_restoring_a_revoked_survey_accepts_responses_again(tmp_path):
    _use_temp_database(tmp_path)
    survey = _survey()
    database.set_survey_revoked(survey["id"], True)
    database.set_survey_revoked(survey["id"], False)
    response = database.append_response(survey["public_token"], "guest-1", [_day(days=7)], ["19:00"], {"cuisine": ["Italian"]})
    assert response["cuisine"] == ["Italian"]
    assert database.get_survey(survey["id"])["status"] == "active"


def test_clearing_the_expiry_reopens_an_expired_survey(tmp_path):
    _use_temp_database(tmp_path)
    survey = _survey(expires_at=_iso(seconds=-1))
    record = database.set_survey_expiry(survey["id"], None)
    assert record["expires_at"] is None
    assert record["status"] == "active"


def test_revoking_does_not_touch_a_configured_expiry(tmp_path):
    _use_temp_database(tmp_path)
    chosen = _iso(days=5)
    survey = _survey(expires_at=chosen)
    database.set_survey_revoked(survey["id"], True)
    assert database.set_survey_revoked(survey["id"], False)["expires_at"] == chosen


def test_set_survey_expiry_rejects_an_unparseable_timestamp(tmp_path):
    _use_temp_database(tmp_path)
    survey = _survey()
    with pytest.raises(ValueError):
        database.set_survey_expiry(survey["id"], "next tuesday")
    assert database.get_survey(survey["id"])["expires_at"] == survey["expires_at"]


def test_revoking_preserves_already_collected_responses(tmp_path):
    _use_temp_database(tmp_path)
    survey = _survey()
    database.append_response(survey["public_token"], "guest-1", [_day(days=7)], ["19:00"], {"cuisine": ["Italian"]})
    database.set_survey_revoked(survey["id"], True)
    assert database.aggregate_survey(survey["id"])["response_count"] == 1


def test_export_carries_answers_timestamps_and_private_origins(tmp_path):
    _use_temp_database(tmp_path)
    survey = _survey()
    database.append_response(
        survey["public_token"], "guest-1", [_day(days=7)], ["19:00"], {"cuisine": ["Italian"]},
        "place-9", "Mission District", 37.7599, -122.4148,
    )
    export = database.export_responses(survey["id"])
    response = export["responses"][0]
    assert export["response_count"] == 1
    assert response["cuisine"] == ["Italian"]
    assert response["origin_label"] == "Mission District"
    assert response["origin_lat"] == 37.7599
    assert response["submitted_at"] and response["updated_at"]


def test_export_omits_the_guest_identifier_that_spans_surveys(tmp_path):
    _use_temp_database(tmp_path)
    survey = _survey()
    database.append_response(survey["public_token"], "guest-1", [_day(days=7)], ["19:00"], {})
    response = database.export_responses(survey["id"])["responses"][0]
    assert "respondent_user_id" not in response
    assert response["response_id"]


def test_aggregate_report_stays_free_of_the_export_only_timestamps(tmp_path):
    _use_temp_database(tmp_path)
    survey = _survey()
    database.append_response(survey["public_token"], "guest-1", [_day(days=7)], ["19:00"], {})
    response = database.aggregate_survey(survey["id"])["report"]["responses"][0]
    assert "submitted_at" not in response


def test_export_of_an_unknown_survey_is_none(tmp_path):
    _use_temp_database(tmp_path)
    assert database.export_responses("does-not-exist") is None
