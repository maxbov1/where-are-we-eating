"""Compact, sanitized observability for one agent invocation."""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Mapping
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from strands.hooks import (
    AfterInvocationEvent,
    AfterModelCallEvent,
    AfterToolCallEvent,
    BeforeModelCallEvent,
    BeforeToolCallEvent,
)

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
    "reservation_sweep": "reservation_scan",
    "reservation_expand": "reservation_inspection",
    "reservation_observe": "reservation_inspection",
    "reservation_fill": "reservation_preparation",
    "reservation_click": "reservation_availability",
    "reservation_prepare": "reservation_preparation",
    "reservation_continue": "reservation_availability",
    "reservation_abandon": "reservation_preparation",
    "reservation_prepare_handoff": "reservation_preparation",
    "reservation_operate": "reservation_availability",
    "reservation_act": "reservation_preparation",
    "reservation_close": "cleanup",
}


def _approx_tokens(value: Any) -> int:
    """Estimate tokens for diagnostics without depending on model-specific tokenizers."""
    try:
        chars = len(json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        chars = len(str(value))
    return max(1, (chars + 3) // 4)


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
        for key in ("handoff_ready", "prefilled", "url_prefilled", "interactive_complete",
                    "availability_verified", "workflow_id", "error_code", "recovery",
                    "continuation_required", "final_action_blocked", "provider",
                    "candidate_id", "booking_url", "prepared_booking_url"):
            if key in payload:
                summary[key] = _sanitize(payload[key], key)
        action_trace = payload.get("action_trace")
        if isinstance(action_trace, list):
            summary["action_trace"] = [
                {key: _sanitize(step.get(key), key)
                 for key in ("step", "action", "success", "label", "control", "error")
                 if key in step}
                for step in action_trace[:20]
                if isinstance(step, Mapping)
            ]
    else:
        summary["preview"] = str(payload)[:240]
    return summary


class AgentTrace:
    """Hook-based trace that records actions without recording model reasoning."""

    def __init__(self, organizer_id: str, on_phase: Callable[[str, str], None] | None = None) -> None:
        self.organizer_id = organizer_id
        self.on_phase = on_phase
        self.previous_state: dict[str, Any] = {}
        self.token_stats = {
            "tool_calls": 0,
            "model_calls": 0,
            "estimated_agent_output_tokens": 0,
            "estimated_tool_context_tokens": 0,
            "largest_tool_context_tokens": 0,
            "projected_input_tokens": 0,
            "largest_projected_input_tokens": 0,
            "cumulative_history_tokens": 0,
            "tool_result_chars": 0,
            "state_snapshot_chars": 0,
            "dom_chars": 0,
            "ax_chars": 0,
            "action_trace_chars": 0,
            "image_bytes": 0,
            "actual_input_tokens": 0,
            "actual_output_tokens": 0,
            "actual_total_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_write_input_tokens": 0,
        }
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
        phase = _PHASES.get(tool, "agent_reasoning")
        if self.on_phase:
            self.on_phase(phase, tool)
        argument_tokens = _approx_tokens(event.tool_use.get("input", {}))
        self.token_stats["tool_calls"] += 1
        self.token_stats["estimated_agent_output_tokens"] += argument_tokens
        self._emit({
            "phase": phase,
            "tool": tool,
            "arguments": event.tool_use.get("input", {}),
            "transition_reason": "agent selected next tool",
            "state_changes": {},
        })

    def after_tool(self, event: AfterToolCallEvent) -> None:
        tool = event.tool_use.get("name", "unknown")
        context_tokens = _approx_tokens(event.result)
        result_payload = _result_payload(event.result)
        result_json = json.dumps(result_payload, ensure_ascii=False, default=str)
        self.token_stats["estimated_tool_context_tokens"] += context_tokens
        self.token_stats["tool_result_chars"] += len(result_json)
        if isinstance(result_payload, Mapping):
            self.token_stats["state_snapshot_chars"] += len(json.dumps(
                result_payload.get("agent_state") or {}, ensure_ascii=False, default=str
            ))
            for key in ("dom", "text"):
                value = result_payload.get(key)
                if value:
                    self.token_stats["dom_chars"] += len(json.dumps(value, ensure_ascii=False, default=str))
            if result_payload.get("accessibility_snapshot"):
                self.token_stats["ax_chars"] += len(str(result_payload["accessibility_snapshot"]))
            if result_payload.get("action_trace"):
                self.token_stats["action_trace_chars"] += len(json.dumps(
                    result_payload["action_trace"], ensure_ascii=False, default=str
                ))
        self.token_stats["image_bytes"] += self._image_bytes(event.result)
        self.token_stats["largest_tool_context_tokens"] = max(
            self.token_stats["largest_tool_context_tokens"], context_tokens
        )
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

    @staticmethod
    def _image_bytes(value: Any) -> int:
        """Best-effort count for image blocks without retaining image data."""
        if isinstance(value, Mapping):
            total = 0
            for key, item in value.items():
                if key == "bytes" and isinstance(item, (bytes, bytearray)):
                    total += len(item)
                else:
                    total += AgentTrace._image_bytes(item)
            return total
        if isinstance(value, (list, tuple)):
            return sum(AgentTrace._image_bytes(item) for item in value)
        return 0

    def before_model(self, event: BeforeModelCallEvent) -> None:
        projected = int(event.projected_input_tokens or 0)
        self.token_stats["model_calls"] += 1
        self.token_stats["projected_input_tokens"] += projected
        self.token_stats["cumulative_history_tokens"] += projected
        self.token_stats["largest_projected_input_tokens"] = max(
            self.token_stats["largest_projected_input_tokens"], projected
        )
        self._emit({
            "phase": "model_call",
            "tool": None,
            "projected_input_tokens": projected,
            "cumulative_history_tokens": self.token_stats["cumulative_history_tokens"],
        })

    def after_model(self, event: AfterModelCallEvent) -> None:
        # Provider usage is surfaced by AgentResult.metrics after invocation;
        # this hook records failures/retries without guessing token counts.
        self._emit({
            "phase": "model_call",
            "tool": None,
            "model_error": str(event.exception)[:240] if event.exception else None,
            "retry": bool(event.retry),
        })

    def after_invocation(self, event: AfterInvocationEvent) -> None:
        result = event.result
        status = getattr(result, "stop_reason", None) or "error"
        metrics = getattr(result, "metrics", None)
        usage = getattr(metrics, "accumulated_usage", None)
        if usage:
            for source, target in (
                ("inputTokens", "actual_input_tokens"),
                ("outputTokens", "actual_output_tokens"),
                ("totalTokens", "actual_total_tokens"),
                ("cacheReadInputTokens", "cache_read_input_tokens"),
                ("cacheWriteInputTokens", "cache_write_input_tokens"),
            ):
                value = usage.get(source) if isinstance(usage, Mapping) else getattr(usage, source, 0)
                self.token_stats[target] = int(value or 0)
        self._emit({
            "phase": "final",
            "tool": None,
            "transition_reason": "agent invocation ended",
            "state_changes": {},
            "final_status": status,
            "token_estimate": self.token_stats,
        })

    def attach(self, agent: Any) -> None:
        agent.add_hook(self.before_tool, BeforeToolCallEvent)
        agent.add_hook(self.after_tool, AfterToolCallEvent)
        agent.add_hook(self.before_model, BeforeModelCallEvent)
        agent.add_hook(self.after_model, AfterModelCallEvent)
        agent.add_hook(self.after_invocation, AfterInvocationEvent)
