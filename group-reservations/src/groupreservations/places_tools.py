"""Google Places tools used for restaurant discovery."""

from __future__ import annotations

import json

from strands import tool

from .adapters.google_places import get_place, search_places
from .config import settings


@tool
def google_places_search(query: str, max_results: int = 5) -> str:
    """Search and hydrate Google Places restaurant candidates.

    The returned records are canonical ``Place`` structs serialized for the
    agent, with opening-hour and source evidence attached. Downstream
    availability checks should use the returned Google ``place_id`` values.
    """
    if not settings.google_places_api_key:
        return json.dumps({"error": "GOOGLE_MAPS_API_KEY is not configured."})
    try:
        candidates = search_places(
            settings.google_places_api_key,
            query=query,
            max_results=max(1, min(max_results, 10)),
        )
        hydrated = []
        hydration_errors = []
        for candidate in candidates:
            try:
                place, evidence = get_place(
                    settings.google_places_api_key, candidate["place_id"]
                )
                hydrated.append({"restaurant": place.__dict__, "evidence": evidence})
            except Exception as exc:  # pragma: no cover - provider-specific failures
                hydration_errors.append(
                    {"place_id": candidate.get("place_id"), "error": str(exc)}
                )
        result = {"source": "Google Places", "restaurants": hydrated}
        if hydration_errors:
            result["hydration_errors"] = hydration_errors
        return json.dumps(result)
    except Exception as exc:  # pragma: no cover - provider-specific failures
        return json.dumps({"error": f"Google Places search failed: {exc}"})


@tool
def google_places_details(place_id: str) -> str:
    """Fetch canonical details and opening-hour evidence for a Google Place."""
    if not settings.google_places_api_key:
        return json.dumps({"error": "GOOGLE_MAPS_API_KEY is not configured."})
    try:
        place, evidence = get_place(settings.google_places_api_key, place_id)
        return json.dumps({"place": place.__dict__, "evidence": evidence})
    except Exception as exc:  # pragma: no cover - provider-specific failures
        return json.dumps({"error": f"Google Places details failed: {exc}"})
