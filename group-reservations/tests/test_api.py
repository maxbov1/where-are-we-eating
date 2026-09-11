from fastapi.testclient import TestClient
from botocore.exceptions import NoCredentialsError

from groupreservations import api


def test_recommendation_credential_error_preserves_cors_headers(monkeypatch):
    """Browser clients receive a useful API error instead of a fake CORS failure."""
    aggregate = {
        "survey_id": "survey-1",
        "event_name": "Team dinner",
        "location": "Seattle",
        "dates": ["2026-09-12"],
        "times": ["19:00"],
        "availability": {"2026-09-12": ["19:00"]},
        "questions": {},
        "report": {},
        "responses": [],
    }
    monkeypatch.setattr(api, "get_survey", lambda identifier: {"id": identifier})
    monkeypatch.setattr(api, "aggregate_survey", lambda identifier: aggregate)
    monkeypatch.setattr(
        api, "run", lambda *args, **kwargs: (_ for _ in ()).throw(NoCredentialsError())
    )

    client = TestClient(api.app, raise_server_exceptions=False)
    response = client.post(
        "/api/surveys/survey-1/recommendations",
        headers={
            "Origin": "http://127.0.0.1:4173",
            "X-Organizer-Id": "local-organizer",
        },
    )

    assert response.status_code == 503
    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:4173"
    assert "AWS" in response.json()["detail"]
