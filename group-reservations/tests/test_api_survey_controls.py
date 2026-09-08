"""Network-free coverage for the organizer's expiration, revoke, and export routes."""

import csv
import io
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from groupreservations import api, database

_SECRET = "test-secret-with-at-least-32-characters"
# Every lifecycle route is organizer-only and scoped to the surveys they own.
_LIFECYCLE_ROUTES = [
    ("post", "/revoke", {"revoked": True}),
    ("post", "/expiration", {"expires_at": None}),
    ("get", "/responses/export", None),
]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("GROUP_RESERVATIONS_JWT_SECRET", _SECRET)
    object.__setattr__(database.settings, "database_path", str(tmp_path / "controls.sqlite3"))
    database.init_db()
    return TestClient(api.app)


def _iso(**delta):
    return (datetime.now(timezone.utc) + timedelta(**delta)).isoformat()


def _day(**delta):
    return (datetime.now(timezone.utc) + timedelta(**delta)).date().isoformat()


def _organizer(ident):
    with database._connect() as db:
        db.execute(
            "INSERT INTO users (id,email,is_temporary,created_at) VALUES (?,?,0,?)",
            (ident, f"{ident}@example.com", database._now()),
        )
    return ident


def _create_survey(client, organizer_id, **overrides):
    body = {
        "event_name": "Dinner", "location": "San Clemente",
        "dates": [_day(days=7)], "times": ["19:00"],
        "questions": {"cuisine": ["Italian", "Japanese"]}, **overrides,
    }
    response = client.post("/api/surveys", json=body, headers={"X-Organizer-Id": organizer_id})
    assert response.status_code == 200, response.text
    return response.json()


def _vote(client, public_token, **overrides):
    return client.post(f"/api/surveys/{public_token}/responses", json={
        "respondent_token": "guest-token-123", "dates": [_day(days=7)],
        "times": ["19:00"], "cuisines": ["Italian"], **overrides,
    })


def _call(client, method, survey_id, path, body, headers=None):
    kwargs = {"headers": headers} if headers else {}
    if body is not None:
        kwargs["json"] = body
    return getattr(client, method)(f"/api/surveys/{survey_id}{path}", **kwargs)


@pytest.mark.parametrize("method,path,body", _LIFECYCLE_ROUTES)
def test_lifecycle_routes_require_an_organizer_identity(client, method, path, body):
    survey = _create_survey(client, _organizer("org-alice"))
    assert _call(client, method, survey["id"], path, body).status_code == 401


@pytest.mark.parametrize("method,path,body", _LIFECYCLE_ROUTES)
def test_lifecycle_routes_are_forbidden_for_a_non_owner(client, method, path, body):
    survey = _create_survey(client, _organizer("org-alice"))
    headers = {"X-Organizer-Id": _organizer("org-eve")}
    assert _call(client, method, survey["id"], path, body, headers).status_code == 403


def test_new_survey_reports_its_derived_lifecycle_state(client):
    created = _create_survey(client, _organizer("org-alice"))["survey"]
    assert created["status"] == "active"
    assert created["expires_at"]
    assert created["revoked_at"] is None


def test_survey_creation_accepts_an_explicit_expiry(client):
    chosen = _iso(days=2)
    created = _create_survey(client, _organizer("org-alice"), expires_at=chosen)["survey"]
    assert created["expires_at"] == chosen


def test_revoking_closes_both_guest_routes(client):
    owner = _organizer("org-alice")
    survey = _create_survey(client, owner)
    assert _vote(client, survey["public_token"]).status_code == 200

    revoked = _call(client, "post", survey["id"], "/revoke", {"revoked": True}, {"X-Organizer-Id": owner})
    assert revoked.status_code == 200
    assert revoked.json()["status"] == "revoked"

    read = client.get(f"/api/surveys/{survey['public_token']}")
    # 410 rather than 404 so the guest UI can say "voting closed", not "not found".
    assert read.status_code == 410
    assert "revoked" in read.json()["detail"]
    assert _vote(client, survey["public_token"]).status_code == 410


def test_restoring_a_revoked_survey_reopens_guest_voting(client):
    owner = _organizer("org-alice")
    survey = _create_survey(client, owner)
    _call(client, "post", survey["id"], "/revoke", {"revoked": True}, {"X-Organizer-Id": owner})
    restored = _call(client, "post", survey["id"], "/revoke", {"revoked": False}, {"X-Organizer-Id": owner})

    assert restored.json()["status"] == "active"
    assert client.get(f"/api/surveys/{survey['public_token']}").status_code == 200
    assert _vote(client, survey["public_token"]).status_code == 200


def test_expiring_a_survey_closes_voting_and_clearing_the_expiry_reopens_it(client):
    owner = _organizer("org-alice")
    survey = _create_survey(client, owner)

    expired = _call(client, "post", survey["id"], "/expiration", {"expires_at": _iso(seconds=-1)}, {"X-Organizer-Id": owner})
    assert expired.json()["status"] == "expired"
    assert _vote(client, survey["public_token"]).status_code == 410

    cleared = _call(client, "post", survey["id"], "/expiration", {"expires_at": None}, {"X-Organizer-Id": owner})
    assert cleared.json()["status"] == "active"
    assert cleared.json()["expires_at"] is None
    assert _vote(client, survey["public_token"]).status_code == 200


def test_expiration_route_rejects_an_unparseable_timestamp(client):
    owner = _organizer("org-alice")
    survey = _create_survey(client, owner)
    response = _call(client, "post", survey["id"], "/expiration", {"expires_at": "next tuesday"}, {"X-Organizer-Id": owner})
    assert response.status_code == 400


def test_closing_a_survey_does_not_lock_out_its_organizer(client):
    owner = _organizer("org-alice")
    survey = _create_survey(client, owner)
    _vote(client, survey["public_token"])
    _call(client, "post", survey["id"], "/revoke", {"revoked": True}, {"X-Organizer-Id": owner})

    aggregate = client.get(f"/api/surveys/{survey['id']}/aggregate", headers={"X-Organizer-Id": owner})
    assert aggregate.status_code == 200
    assert aggregate.json()["response_count"] == 1
    assert aggregate.json()["status"] == "revoked"

    export = client.get(f"/api/surveys/{survey['id']}/responses/export", headers={"X-Organizer-Id": owner})
    assert export.status_code == 200
    assert export.json()["response_count"] == 1


def test_export_exposes_the_guest_origins_that_the_aggregate_hides(client):
    owner = _organizer("org-alice")
    survey = _create_survey(client, owner)
    _vote(client, survey["public_token"], origin_label="Mission District", origin_place_id="place-9")

    export = client.get(f"/api/surveys/{survey['id']}/responses/export", headers={"X-Organizer-Id": owner}).json()
    assert export["responses"][0]["origin_label"] == "Mission District"
    assert "respondent_user_id" not in export["responses"][0]

    aggregate = client.get(f"/api/surveys/{survey['id']}/aggregate", headers={"X-Organizer-Id": owner}).json()
    assert "origin_label" not in aggregate["responses"][0]


def test_csv_export_is_a_download_with_one_row_per_response(client):
    owner = _organizer("org-alice")
    survey = _create_survey(client, owner)
    _vote(client, survey["public_token"])

    response = client.get(f"/api/surveys/{survey['id']}/responses/export?format=csv", headers={"X-Organizer-Id": owner})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert f'filename="survey-{survey["id"]}-responses.csv"' in response.headers["content-disposition"]

    rows = list(csv.reader(io.StringIO(response.text)))
    assert rows[0][:3] == ["response_id", "submitted_at", "updated_at"]
    assert len(rows) == 2
    assert rows[1][rows[0].index("cuisine")] == "Italian"
    assert rows[1][rows[0].index("availability")] == f"{_day(days=7)}: 19:00"


def test_export_rejects_an_unknown_format(client):
    owner = _organizer("org-alice")
    survey = _create_survey(client, owner)
    response = client.get(f"/api/surveys/{survey['id']}/responses/export?format=xml", headers={"X-Organizer-Id": owner})
    assert response.status_code == 422
