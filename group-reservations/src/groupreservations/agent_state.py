"""Serializable state and action affordances for one agent invocation."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class AgentState:
    """Small public state contract shared by the agent and its tools.

    This is operational state, not chain-of-thought. It records observations
    and transitions that another tool or a human can safely inspect.
    """

    phase: str = "start"
    status: str = "running"
    survey_id: str | None = None
    group_location: str | None = None
    current_candidate: dict[str, Any] = field(default_factory=dict)
    scanned_candidates: dict[str, dict[str, Any]] = field(default_factory=dict)
    browser: dict[str, Any] = field(default_factory=dict)
    reservation: dict[str, Any] = field(default_factory=dict)
    verification: dict[str, Any] = field(default_factory=dict)
    available_actions: list[dict[str, str]] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    last_error: str | None = None
    observations: list[dict[str, Any]] = field(default_factory=list)

    def transition(self, phase: str, reason: str, **changes: Any) -> None:
        self.phase = phase
        self.observations.append({"phase": phase, "reason": reason})
        if len(self.observations) > 12:
            self.observations = self.observations[-12:]
        for key, value in changes.items():
            if hasattr(self, key):
                setattr(self, key, value)

    def set_actions(self, *actions: tuple[str, str]) -> None:
        self.available_actions = [
            {"tool": tool, "reason": reason} for tool, reason in actions
        ]

    def snapshot(self) -> dict[str, Any]:
        """Return JSON-safe state for logs and the agent evidence tool."""
        return asdict(self)
