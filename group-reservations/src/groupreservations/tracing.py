"""Compact, sanitized observability for one agent invocation."""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from strands.hooks import AfterInvocationEvent, AfterToolCallEvent, BeforeToolCallEvent

# Use the agent runner's configured logger so trace records appear alongside
# the existing lifecycle messages in the local test entrypoint.
logger = logging.getLogger("groupreservations.opentable_mcp")
_SECRET = re.compile(r"token|secret|password|cookie|authorization|credential|api[_-]?key", re.I)
_PHASES = {
    "survey_get_evidence": "evidence_recovery",
    "google_places_search": "restaurant_discovery",
    "google_places_details": "restaurant_hydration",
    "reservation_open": "reservation_scan",
    "reservation_scan_dom": "reservation_scan",
    "reservation_fill": "reservation_preparation",
    "reservation_click": "reservation_availability",
    "reservation_prepare": "reservation_preparation",
    "reservation_close": "cleanup",
}


def _safe_url(value: str) -> str:
    parts = urlsplit(value)
    if not parts.scheme or not parts.netloc:
        return value[:240]
    query = [(key, "[redacted]") for key, _ in parse_qsl(parts.query, keep_blank_values=True)]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def _sanitize(value: Any, key: str = "") -> Any:
    if _SECRET.search(key):
        return "[redacted]"
    if isinstance(value, str):
        if value.startswith(("http://", "https://")):
            return _safe_url(value)
        return value[:240]
    if isinstance(value, Mapping):
        return {str(name): _sanitize(item, str(name)) for name, item in list(value.items())[:30]}
    if isinstance(value, (list, tuple)):
        return [_sanitize(item, key) for item in list(value)[:30]]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:240]


def _result_payload(result: Any) -> Any:
    if isinstance(result, Mapping):
        content = result.get("content")
        if isinstance(content, list):
            text = " ".join(str(item.get("text", "")) for item in content if isinstance(item, Mapping))
            try:
                return json.loads(text)
            except (TypeError, json.JSONDecodeError):
                return {"text": text[:500]}
    return result


def _summary(result: Any) -> dict[str, Any]:
    payload = _result_payload(result)
    summary: dict[str, Any] = {"type": type(payload).__name__}
    if isinstance(payload, Mapping):
        summary["success"] = payload.get("success")
        summary["status"] = payload.get("status")
        if payload.get("error"):
            summary["error"] = str(payload["error"])[:240]
        if payload.get("text"):
            summary["text"] = str(payload["text"])[:500]
        summary["keys"] = list(payload.keys())[:20]
        evidence_ids = []
        source_urls = []
        for key, value in payload.items():
            if re.search(r"(?:^|_)(?:id|place_id|survey_id)$", str(key), re.I) and value:
                evidence_ids.append(f"{key}:{value}")
            if re.search(r"(?:url|uri|source)", str(key), re.I) and isinstance(value, str):
                source_urls.append(_safe_url(value))
        if evidence_ids:
            summary["evidence_ids"] = evidence_ids[:20]
        if source_urls:
            summary["source_urls"] = source_urls[:20]
    else:
        summary["preview"] = str(payload)[:240]
    return summary


class AgentTrace:
    """Hook-based trace that records actions without recording model reasoning."""

    def __init__(self, organizer_id: str) -> None:
        self.organizer_id = organizer_id
        self.previous_state: dict[str, Any] = {}
        # Debug mode should be sufficient to explain a stuck run. TRACE can be
        # enabled independently when callers want structured events without
        # the rest of the verbose logs.
        self.enabled = bool(
            os.getenv("GROUP_RESERVATIONS_TRACE") or os.getenv("GROUP_RESERVATIONS_DEBUG")
        )

    def _emit(self, record: dict[str, Any]) -> None:
        if self.enabled:
            logger.info("agent_trace %s", json.dumps(_sanitize(record), separators=(",", ":")))

    def before_tool(self, event: BeforeToolCallEvent) -> None:
        tool = event.tool_use.get("name", "unknown")
        self._emit({
            "phase": _PHASES.get(tool, "agent_reasoning"),
            "tool": tool,
            "arguments": event.tool_use.get("input", {}),
            "transition_reason": "agent selected next tool",
            "state_changes": {},
        })

    def after_tool(self, event: AfterToolCallEvent) -> None:
        tool = event.tool_use.get("name", "unknown")
        result = _summary(event.result)
        state_changes = {key: result[key] for key in ("success", "status", "evidence_ids", "source_urls") if key in result}
        self._emit({
            "phase": _PHASES.get(tool, "agent_reasoning"),
            "tool": tool,
            "tool_result_summary": result,
            "evidence_ids": result.get("evidence_ids", []),
            "source_urls": result.get("source_urls", []),
            "transition_reason": "tool completed; agent loop continues",
            "state_changes": state_changes,
        })

    def after_invocation(self, event: AfterInvocationEvent) -> None:
        result = event.result
        status = getattr(result, "stop_reason", None) or "error"
        self._emit({
            "phase": "final",
            "tool": None,
            "transition_reason": "agent invocation ended",
            "state_changes": {},
            "final_status": status,
        })

    def attach(self, agent: Any) -> None:
        agent.add_hook(self.before_tool, BeforeToolCallEvent)
        agent.add_hook(self.after_tool, AfterToolCallEvent)
        agent.add_hook(self.after_invocation, AfterInvocationEvent)
