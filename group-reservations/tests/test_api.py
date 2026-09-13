from fastapi.testclient import TestClient
from groupreservations import api


def test_recommendation_starts_background_run_with_cors_headers(monkeypatch):
    """Browser clients receive a run handle instead of waiting on the agent."""
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
    monkeypatch.setattr(
        api,
        "get_survey",
        lambda identifier: {"id": identifier, "organizer_id": "local-organizer"},
    )
    monkeypatch.setattr(api, "aggregate_survey", lambda identifier: aggregate)
    monkeypatch.setattr(api, "invoke_agentcore", lambda *args, **kwargs: "demo answer")

    client = TestClient(api.app, raise_server_exceptions=False)
    response = client.post(
        "/api/surveys/survey-1/recommendations",
        headers={
            "Origin": "http://127.0.0.1:4173",
            "X-Organizer-Id": "local-organizer",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:4173"
    assert response.json()["status"] == "queued"
    assert response.json()["run_id"].startswith("recommendation-")


def test_public_recommendation_run_excludes_raw_agent_answer_and_internal_context():
    public = api._public_recommendation_run({
        "run_id": "recommendation-test",
        "status": "complete",
        "response": {"recommendation": {"primary": {"name": "Pronto"}}},
        "answer": "private model trace and prompt",
        "organizer_id": "private-organizer",
        "_prompt": "private research prompt",
        "_state": object(),
    })

    assert public["run_id"] == "recommendation-test"
    assert "answer" not in public
    assert "organizer_id" not in public
    assert "_prompt" not in public
    assert "_state" not in public
