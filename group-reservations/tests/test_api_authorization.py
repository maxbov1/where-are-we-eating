"""Network-free coverage for organizer authorization on survey-management routes."""

import pytest
from fastapi.testclient import TestClient

from groupreservations import api, database
from groupreservations.auth import mint_access_token

_SECRET = "test-secret-with-at-least-32-characters"
_SURVEY_BODY = {
    "event_name": "Dinner",
    "location": "San Clemente",
    "dates": ["2026-09-04"],
    "times": ["19:00"],
    "questions": {"cuisine": ["Italian", "Japanese"]},
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("GROUP_RESERVATIONS_JWT_SECRET", _SECRET)
    object.__setattr__(database.settings, "database_path", str(tmp_path / "api.sqlite3"))
    database.init_db()
    return TestClient(api.app)


def _organizer(ident):
    """Insert an organizer row so the surveys foreign key is satisfiable."""
    with database._connect() as db:
        db.execute(
            "INSERT INTO users (id,email,is_temporary,created_at) VALUES (?,?,0,?)",
            (ident, f"{ident}@example.com", database._now()),
        )
    return ident


def _create_survey(client, organizer_id):
    response = client.post(
        "/api/surveys", json=_SURVEY_BODY, headers={"X-Organizer-Id": organizer_id}
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_create_survey_requires_an_organizer_identity(client):
    response = client.post("/api/surveys", json=_SURVEY_BODY)
    assert response.status_code == 401


def test_create_survey_with_legacy_header_records_that_owner(client):
    organizer = _organizer("org-alice")
    body = client.post(
        "/api/surveys", json=_SURVEY_BODY, headers={"X-Organizer-Id": organizer}
    ).json()
    assert body["survey"]["organizer_id"] == organizer


def test_create_survey_binds_ownership_to_the_verified_token_subject(client):
    organizer = _organizer("org-bob")
    token = mint_access_token(organizer)
    body = client.post(
        "/api/surveys", json=_SURVEY_BODY, headers={"Authorization": f"Bearer {token}"}
    ).json()
    assert body["survey"]["organizer_id"] == organizer


def test_create_survey_rejects_a_non_bearer_authorization_scheme(client):
    response = client.post(
        "/api/surveys", json=_SURVEY_BODY, headers={"Authorization": "Token abc"}
    )
    assert response.status_code == 401


def test_create_survey_rejects_an_unverifiable_bearer_token(client):
    response = client.post(
        "/api/surveys", json=_SURVEY_BODY, headers={"Authorization": "Bearer not-a-real-token"}
    )
    assert response.status_code == 401


def test_aggregate_requires_an_organizer_identity(client):
    survey = _create_survey(client, _organizer("org-alice"))
    response = client.get(f"/api/surveys/{survey['id']}/aggregate")
    assert response.status_code == 401


def test_aggregate_is_forbidden_for_a_different_organizer(client):
    survey = _create_survey(client, _organizer("org-alice"))
    intruder = _organizer("org-eve")
    response = client.get(
        f"/api/surveys/{survey['id']}/aggregate", headers={"X-Organizer-Id": intruder}
    )
    assert response.status_code == 403


def test_aggregate_returns_the_report_to_the_owning_organizer(client):
    owner = _organizer("org-alice")
    survey = _create_survey(client, owner)
    response = client.get(
        f"/api/surveys/{survey['id']}/aggregate", headers={"X-Organizer-Id": owner}
    )
    assert response.status_code == 200
    assert response.json()["survey_id"] == survey["id"]


def test_aggregate_of_an_unknown_survey_is_not_found(client):
    response = client.get(
        "/api/surveys/does-not-exist/aggregate", headers={"X-Organizer-Id": _organizer("org-alice")}
    )
    assert response.status_code == 404


def test_guest_survey_read_and_response_routes_stay_public(client):
    survey = _create_survey(client, _organizer("org-alice"))
    public_token = survey["public_token"]

    read = client.get(f"/api/surveys/{public_token}")
    assert read.status_code == 200
    assert "organizer_id" not in read.json()

    submitted = client.post(
        f"/api/surveys/{public_token}/responses",
        json={
            "respondent_token": "guest-token-123",
            "dates": ["2026-09-04"],
            "times": ["19:00"],
            "cuisines": ["Italian"],
        },
    )
    assert submitted.status_code == 200


def test_survey_recommendations_is_forbidden_for_a_non_owner_before_any_agent_call(client):
    survey = _create_survey(client, _organizer("org-alice"))
    intruder = _organizer("org-eve")
    response = client.post(
        f"/api/surveys/{survey['id']}/recommendations", headers={"X-Organizer-Id": intruder}
    )
    assert response.status_code == 403


def test_recommendations_rejects_an_unauthenticated_request_before_any_agent_call(client):
    survey = _create_survey(client, _organizer("org-alice"))
    response = client.post(
        "/api/recommendations",
        json={
            "survey_id": survey["id"],
            "event_name": "Dinner",
            "location": "San Clemente",
            "dates": ["2026-09-04"],
            "times": ["19:00"],
            "responses": [],
        },
    )
    assert response.status_code == 401
