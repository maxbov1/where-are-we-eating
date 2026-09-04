import json

from groupreservations.agent_state import AgentState


def test_agent_state_is_serializable_and_exposes_action_affordances():
    state = AgentState(survey_id="survey-1", group_location="San Clemente")
    state.transition("reservation_scan", "page opened", browser={"url": "https://example.test"})
    state.set_actions(
        ("reservation_scan_dom", "Scan the rendered page"),
        ("reservation_close", "End the browser session"),
    )

    snapshot = state.snapshot()
    assert snapshot["phase"] == "reservation_scan"
    assert snapshot["browser"]["url"] == "https://example.test"
    assert [item["tool"] for item in snapshot["available_actions"]] == [
        "reservation_scan_dom", "reservation_close"
    ]
    assert json.loads(json.dumps(snapshot))["survey_id"] == "survey-1"
