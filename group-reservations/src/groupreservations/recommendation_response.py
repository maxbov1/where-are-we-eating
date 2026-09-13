"""Parse the agent's compact recommendation into a UI-safe response envelope.

The agent remains responsible for the recommendation prose.  This module is a
small deterministic adapter at the HTTP boundary so the frontend does not
have to infer workflow state from arbitrary text.
"""

from __future__ import annotations

import re
import json
from urllib.parse import urlsplit
from typing import Any
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


_MARKDOWN_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", re.I)
_PLAIN_URL = re.compile(r"https?://[^\s<>\]\)\"']+", re.I)
_BOOKING_WORDS = re.compile(r"book|reserv|opentable|resy|tock|find a table", re.I)
_CONFIRM_WORDS = re.compile(r"confirm reservation|confirm booking|final booking", re.I)


class AvailabilityEvidence(BaseModel):
    """Evidence state shown beside a restaurant recommendation."""

    model_config = ConfigDict(extra="ignore")
    status: Literal["verified", "unknown", "unavailable"] = "unknown"
    summary: str = Field(default="Availability was not verified.", max_length=500)
    checked_at: str | None = None
    source_url: str | None = None


class ReservationHandoff(BaseModel):
    """A safe external handoff; this never means a reservation was made."""

    model_config = ConfigDict(extra="ignore")
    status: Literal["available", "unknown", "unavailable"] = "unknown"
    url: str | None = None
    provider: str | None = None
    label: str = Field(default="Open reservation options", max_length=160)


class RestaurantRecommendation(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str = Field(min_length=1, max_length=160)
    restaurant_url: str | None = None
    description: str = Field(default="", max_length=500)
    traits: list[str] = Field(default_factory=list, max_length=6)
    tradeoff: str = Field(default="", max_length=500)
    availability: AvailabilityEvidence = Field(default_factory=AvailabilityEvidence)
    reservation: ReservationHandoff = Field(default_factory=ReservationHandoff)


class SuggestedAction(BaseModel):
    """Allowlisted, non-destructive action suggested by the agent."""

    model_config = ConfigDict(extra="ignore")
    id: Literal["show_alternatives", "adjust_preferences", "refresh_research"]
    label: str = Field(min_length=1, max_length=120)
    kind: Literal["follow_up"] = "follow_up"


class RecommendationContract(BaseModel):
    """The model-to-UI contract for one recommendation result."""

    model_config = ConfigDict(extra="ignore")
    status: Literal["ready", "blocked"] = "ready"
    group_fit: str = Field(default="", max_length=700)
    primary: RestaurantRecommendation
    alternatives: list[RestaurantRecommendation] = Field(default_factory=list, max_length=2)
    blocker: dict[str, str] | None = None
    next_steps: list[str] = Field(default_factory=list, max_length=3)
    actions: list[SuggestedAction] = Field(default_factory=list, max_length=3)


def _safe_url(value: str) -> str | None:
    """Accept only absolute HTTP(S) URLs for rendered external actions."""
    candidate = value.strip().rstrip(".,;:↗").rstrip()
    parts = urlsplit(candidate)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return None
    return candidate


def _structured_recommendation(text: str) -> dict[str, Any] | None:
    """Validate the small JSON envelope used by the recommendation UI."""
    candidate = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", candidate, re.I | re.S)
    candidates = [fenced.group(1)] if fenced else []
    candidates.append(candidate)
    if not fenced:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start >= 0 and end > start:
            candidates.insert(0, candidate[start:end + 1])
    payload = None
    for candidate_json in candidates:
        try:
            decoded = json.loads(candidate_json)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(decoded, dict) and isinstance(decoded.get("primary"), dict):
            payload = decoded
            break
    if not isinstance(payload, dict) or not isinstance(payload.get("primary"), dict):
        return None
    # Accept the previous draft shape during the rollout.  The public result
    # is still emitted only in the new nested contract shape.
    for option in [payload["primary"], *payload.get("alternatives", [])]:
        if not isinstance(option, dict):
            continue
        if isinstance(option.get("availability"), str):
            option["availability"] = {"summary": option["availability"]}
        if "reservation" not in option and (option.get("booking_url") or option.get("booking_label")):
            option["reservation"] = {
                "url": option.get("booking_url"),
                "label": option.get("booking_label") or f"Get {option.get('name', 'restaurant')}'s reservation",
                "status": "unknown",
            }
    try:
        contract = RecommendationContract.model_validate(payload)
    except ValidationError:
        return None

    result = contract.model_dump()
    options = [result["primary"], *result["alternatives"]]
    for option in options:
        option["restaurant_url"] = _safe_url(option.get("restaurant_url") or "")
        evidence = option["availability"]
        evidence["source_url"] = _safe_url(evidence.get("source_url") or "")
        reservation = option["reservation"]
        reservation["url"] = _safe_url(reservation.get("url") or "")
        if reservation["url"] is None:
            reservation["status"] = "unavailable"
    return result


def parse_recommendation_answer(answer: str) -> dict[str, Any]:
    """Return deterministic links and safe UI actions extracted from prose."""
    text = answer if isinstance(answer, str) else str(answer or "")
    recommendation = _structured_recommendation(text)
    links: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(label: str, raw_url: str, context: str = "") -> None:
        url = _safe_url(raw_url)
        if not url or url in seen:
            return
        seen.add(url)
        label = re.sub(r"\s+", " ", label).strip() or "Open link"
        context = f"{label} {context} {url}"
        if _CONFIRM_WORDS.search(context):
            kind = "confirmation"
        elif _BOOKING_WORDS.search(context):
            kind = "handoff"
        else:
            kind = "source"
        links.append({"label": label[:120], "url": url, "kind": kind})

    if recommendation:
        options = [recommendation["primary"], *recommendation["alternatives"]]
        for option in options:
            if option.get("restaurant_url"):
                add(option["name"], option["restaurant_url"], "restaurant website")
            if option["reservation"].get("url"):
                add(option["reservation"].get("label") or f"Get {option['name']} reservation", option["reservation"]["url"], "booking reservation")

    for match in _MARKDOWN_LINK.finditer(text):
        start, end = match.span()
        add(match.group(1), match.group(2), text[max(0, start - 160):min(len(text), end + 160)])

    # Also recognize links in the fallback/plain-text answer.  URLs already
    # captured from Markdown are deduplicated above.
    for match in _PLAIN_URL.finditer(text):
        start, end = match.span()
        line = text[text.rfind("\n", 0, start) + 1:text.find("\n", end) if text.find("\n", end) >= 0 else len(text)]
        add("Booking link" if _BOOKING_WORDS.search(line) else "Open source", match.group(), line)

    actions: list[dict[str, str]] = []
    if recommendation:
        for index, option in enumerate([recommendation["primary"], *recommendation["alternatives"]]):
            if option["reservation"].get("url"):
                actions.append({
                    "id": "get_primary_reservation" if index == 0 else f"get_alternative_reservation_{index}",
                    "label": option["reservation"].get("label") or f"Get {option['name']} reservation",
                    "kind": "handoff",
                    "url": option["reservation"]["url"],
                })
        actions.extend(recommendation.get("actions", []))
    if not recommendation:
        confirmation = next((item for item in links if item["kind"] == "confirmation"), None)
        if confirmation:
            actions.append({"id": "confirm_reservation", "label": "Review and confirm reservation", "kind": "confirmation", "url": confirmation["url"]})

        # Legacy prose has no reliable primary/alternative structure. Keep
        # the first booking URL as the single prominent action; all URLs in
        # the draft remain clickable in the rendered answer.
        handoffs = [item for item in links if item["kind"] == "handoff"][:1]
        for index, link in enumerate(handoffs):
            actions.append({"id": f"booking_handoff_{index + 1}", "label": link["label"], "kind": "handoff", "url": link["url"]})

    # These are UI intents, not claims that an operation has happened.  The
    # organizer can use them to start the next request in the chat/agent UI.
    if not recommendation or recommendation.get("status") != "blocked":
        existing_ids = {action["id"] for action in actions}
        actions.extend([
            action for action in [
                {"id": "show_alternatives", "label": "Show other options", "kind": "follow_up"},
                {"id": "adjust_preferences", "label": "Adjust group preferences", "kind": "follow_up"},
            ] if action["id"] not in existing_ids
        ])
    result = {"links": links, "actions": actions}
    if recommendation:
        result["recommendation"] = recommendation
    return result
