"""Google Places tools used for restaurant discovery."""

from __future__ import annotations

import json
import logging
import time

from strands import tool

from .adapters.google_places import get_place, search_places
from .config import settings

logger = logging.getLogger("groupreservations.agent")


@tool
def google_places_search(query: str, max_results: int = 5) -> str:
    """Search Google Places and return compact candidate identities.

    Details and reservation evidence are intentionally fetched only after the
    agent selects candidates. This keeps discovery cheap and prevents the
    same restaurant data from being serialized again by google_places_details.
    """
    if not settings.google_places_api_key:
        return json.dumps({"error": "GOOGLE_MAPS_API_KEY is not configured."})
    started_at = time.monotonic()
    logger.info("places tool_start tool=google_places_search max_results=%s", max_results)
    try:
        candidates = search_places(
            settings.google_places_api_key,
            query=query,
            max_results=max(1, min(max_results, 10)),
        )
        return json.dumps({
            "source": "Google Places",
            "restaurants": candidates,
            "next_step": "Hydrate selected place_id values with google_places_details.",
        }, separators=(",", ":"))
    except Exception as exc:  # pragma: no cover - provider-specific failures
        return json.dumps({"error": f"Google Places search failed: {exc}"})
    finally:
        logger.info("places tool_end tool=google_places_search duration_ms=%.1f", (time.monotonic() - started_at) * 1000)


@tool
def google_places_details(place_id: str) -> str:
    """Fetch canonical details and opening-hour evidence for a Google Place."""
    if not settings.google_places_api_key:
        return json.dumps({"error": "GOOGLE_MAPS_API_KEY is not configured."})
    started_at = time.monotonic()
    logger.info("places tool_start tool=google_places_details place_id=%s", place_id)
    try:
        place, evidence = get_place(settings.google_places_api_key, place_id)
        return json.dumps({"place": place.__dict__, "evidence": evidence})
    except Exception as exc:  # pragma: no cover - provider-specific failures
        return json.dumps({"error": f"Google Places details failed: {exc}"})
    finally:
        logger.info("places tool_end tool=google_places_details place_id=%s duration_ms=%.1f", place_id, (time.monotonic() - started_at) * 1000)
