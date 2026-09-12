"""Parse the agent's compact recommendation into a UI-safe response envelope.

The agent remains responsible for the recommendation prose.  This module is a
small deterministic adapter at the HTTP boundary so the frontend does not
have to infer workflow state from arbitrary text.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit
from typing import Any


_MARKDOWN_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", re.I)
_PLAIN_URL = re.compile(r"https?://[^\s<>\]\)\"']+", re.I)
_BOOKING_WORDS = re.compile(r"book|reserv|opentable|resy|tock|find a table", re.I)
_CONFIRM_WORDS = re.compile(r"confirm reservation|confirm booking|final booking", re.I)


def _safe_url(value: str) -> str | None:
    """Accept only absolute HTTP(S) URLs for rendered external actions."""
    candidate = value.rstrip(".,;:")
    parts = urlsplit(candidate)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return None
    return candidate


def parse_recommendation_answer(answer: str) -> dict[str, Any]:
    """Return deterministic links and safe UI actions extracted from prose."""
    text = answer if isinstance(answer, str) else str(answer or "")
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
    confirmation = next((item for item in links if item["kind"] == "confirmation"), None)
    if confirmation:
        actions.append({"id": "confirm_reservation", "label": "Review and confirm reservation", "kind": "confirmation", "url": confirmation["url"]})

    handoffs = [item for item in links if item["kind"] == "handoff"]
    for index, link in enumerate(handoffs):
        actions.append({"id": f"booking_handoff_{index + 1}", "label": link["label"], "kind": "handoff", "url": link["url"]})

    # These are UI intents, not claims that an operation has happened.  The
    # organizer can use them to start the next request in the chat/agent UI.
    actions.extend([
        {"id": "show_alternatives", "label": "Show other options", "kind": "follow_up"},
        {"id": "adjust_preferences", "label": "Adjust group preferences", "kind": "follow_up"},
    ])
    return {"links": links, "actions": actions}
