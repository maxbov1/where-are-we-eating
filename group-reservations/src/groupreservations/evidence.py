"""Read-only evidence access for agent tool calls."""

from __future__ import annotations

from typing import Any

from .database import aggregate_survey, get_survey


def get_survey_evidence(identifier: str, organizer_id: str) -> dict[str, Any]:
    """Return the deterministic group summary without exposing guest origins."""
    survey = get_survey(identifier)
    if not survey:
        return {"success": False, "error": "Survey not found."}
    if survey["organizer_id"] != organizer_id:
        return {"success": False, "error": "Survey evidence is not available to this organizer."}

    aggregate = aggregate_survey(survey["id"])
    if not aggregate:
        return {"success": False, "error": "Survey aggregate is unavailable."}
    report = aggregate["report"]
    return {
        "success": True,
        "survey_id": aggregate["survey_id"],
        "event": report["event"],
        "response_count": report["response_count"],
        "schedule": report["schedule"],
        "preferences": report["preferences"],
        "confidence": report["confidence"],
    }
