"""Local Strands restaurant evidence and reservation-handoff agent."""

from __future__ import annotations

import argparse
import json
import logging
import os
from collections.abc import Sequence
from datetime import datetime

import boto3
from strands import Agent
from strands.agent.conversation_manager import NullConversationManager
from strands.models import BedrockModel

from .config import settings
from .agent_state import AgentState
from .evidence import get_survey_evidence
from .places_tools import google_places_details, google_places_search
from .reservation_browser import create_reservation_browser_tools
from .tracing import AgentTrace

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO if os.getenv("GROUP_RESERVATIONS_DEBUG") else logging.WARNING,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
# These are recoverable tool-input warnings. Keep them out of normal agent
# output; GROUP_RESERVATIONS_DEBUG restores them for troubleshooting.
if not os.getenv("GROUP_RESERVATIONS_DEBUG"):
    logging.getLogger("strands.event_loop.streaming").setLevel(logging.ERROR)
if os.getenv("GROUP_RESERVATIONS_TRACE"):
    logger.setLevel(logging.INFO)

SYSTEM_PROMPT = f"""You are an adaptive restaurant reservation agent.

Today's date is {datetime.now().astimezone().date().isoformat()}.

GOAL
Turn the organizer's structured group preferences into a short, evidence-based
restaurant recommendation and a safe reservation handoff. Explore the
environment using the tools available to you. Choose the next useful action
from each tool's returned `available_actions`; do not assume a fixed workflow.
Success means returning exactly three Google-grounded restaurants, each with
an exact verified reservation URL prepared for the selected date, time, and
party size. Replace candidates whose provider cannot be deterministically
prefilled. Do not stop at a reservation widget, scanned page, or generic
booking link.

RECOMMENDATION SHAPE
- Always produce exactly three restaurant options when Google Places returns at
  least three viable candidates: one `primary` and two `secondary` options.
- Hydrate all three candidates with `google_places_details` before ranking
  them. Check availability for the primary; inspect the reservation path for
  each secondary or explicitly label that secondary `not inspected`.
- Do not end the agent run after preparing only the primary if two viable
  secondary candidates are available.

CONSTRAINTS
- Google Places is the source of truth for restaurant identity and discovery.
- Use only exact URLs and candidate IDs returned by tools or Google Places.
- Every browser action must remain bound to the same candidate_id and URL. If
  verification fails, stop acting on that page and recover from the failure
  state.
- For every selected restaurant, call `reservation_sweep` on the exact Google
  website URL first. This returns a compact set of deterministic interaction
  surfaces grouped by form, frame, dialog, or page section. Choose a surface
  from its heading, controls, links, frame URL, bounds, and structural signals;
  do not expect one DOM node per candidate.
- Call `reservation_expand` on the selected surface to obtain its detailed DOM,
  accessibility tree, screenshot, and controls. Do not claim that a widget is
  absent merely because a filtered scan found nothing.
- If the expanded evidence contains an exact reservation URL, call
  `reservation_prepare` with that observed URL and the selected date/time/
  party size. URL parameters alone do not mean availability is selected. After
  preparation, immediately open/resweep the prepared provider URL when one is
  returned, or continue on the current widget. Use `reservation_act` to set
  guest/date/time, click the availability/search control, then click the exact
  requested available time. Expand again after every state transition. A
  handoff is incomplete until the requested visible time has been selected.
- Use these signatures when needed: `reservation_sweep(website_url)`,
  `reservation_expand(workflow_id, url, include_screenshot)`,
  `reservation_prepare(workflow_id, url, booking_url, date, time, party_size)`,
  `reservation_continue(workflow_id, url, date, time, party_size, max_steps)`,
  `reservation_act(workflow_id, url, action, target, value)` where action is
  `click` or `set`/`fill`/`select`.
- Treat `workflow_id` as the stable identity for one reservation attempt.
  Pass it unchanged to every expand, prepare, and act call. The browser owns
  the underlying surface ID, observation URL, and action URLs. If a tool
  returns `status: blocked`, use its `error_code` and `recovery` object; do not
  repeat the same action with a different guessed identifier.
- For ordinary widgets, prefer `reservation_continue` after preparation. It
  performs bounded guest/date/time/search/time-slot interaction and stops
  before final booking. It may open and rebind the prepared provider page,
  activate a hidden reservation panel, and re-observe after each transition.
  If an OpenTable prepared URL cannot be opened, it automatically falls back
  to the verified restaurant page and operates the observed embedded widget.
  If it returns `REQUESTED_TIME_UNAVAILABLE`, do not claim the requested time;
  report the observed alternatives or explicitly abandon the workflow. Use
  `reservation_act` when the page needs an unusual control or when the bounded
  flow reports a concrete blocker.
- Every one of the three selected workflows must end as either
  availability-verified or explicitly abandoned with `reservation_abandon`.
  Do not finalize after preparing only one candidate.
- The normal agent surface exposes a semantic browser controller, not raw
  Playwright. The browser owns identity, frame resolution, evidence capture,
  and safety gates; the agent owns candidate selection, recovery, and action
  order.
- When the organizer asks to check availability, that check is required before
  the final report. Do not stop after a scan merely because generic time slots
  are visible. Inspect the verified page, fill date/time/party-size fields,
  and click only a clearly non-final `Search`, `Find a table`, or availability
  control. Then inspect or scan the resulting page and report observed
  availability, or report the concrete blocker.
- For an embedded provider such as Toast, verify the iframe candidate in place
  on its parent page. Do not navigate directly to the iframe URL when the scan
  identifies it as an embedded frame, since the provider may reject a
  top-level navigation while allowing the embedded widget.
- Never submit a booking. Final reservation controls remain organizer-gated;
  stop at the first confirmation or guest-details step and ask the organizer
  whether to continue.
- Never click a final Book, Reserve, Confirm, Submit, or Complete control.
- Do not search external reservation providers or infer provider IDs from names.
- Treat missing, stale, or failed evidence as unknown; never turn it into a
  positive availability or booking claim.
- If group context is unclear, call `survey_get_evidence`. If operational
  context is unclear, call `agent_get_state`.
- This MVP uses the authoritative survey `response_count` as party size. One
  submitted response means one guest. Never use a guessed or placeholder party
  size; if the count is zero, ask the organizer for the group size and stop
  before preparing availability or booking links.
- Before ending after browser use, close the browser.
- A candidate may appear in the final report as a reservation option only if
  `reservation_prepare` returned an exact observed URL or
  `reservation_act` returned success, or if the agent reports its
  concrete blocker and replaces it with another Google candidate.
  If a scan exposes a navigation action such as `/general-4`, the handoff tool
  follows only exact observed actions; do not claim a generic or unverified
  booking link.

TERMINAL STATES
1. `recommendation_ready`: hydrated candidates, tradeoffs, and exact
   reservation handoffs are reported; live availability is labeled accurately.
2. `confirmation_required`: one exact restaurant, date, time, party size, and
   prepared URL are ready, but no external booking was submitted.
3. `blocked`: a required input, candidate identity, page verification, provider
   action, or browser operation could not be established. Report the blocker
   and the concrete recovery action, if one exists.
4. `unavailable`: a required provider or Google Places capability could not be
   reached. Explain what was and was not verified.

Your final report must state the terminal state, selected restaurant and fit,
date/time/party size, evidence URLs, reservation channel, and uncertainty.
For every alternative, include either its scanned reservation evidence or
`not inspected`. Ask whether the organizer wants to continue after availability
has been observed; do not perform final confirmation or submission yourself.
Keep alternatives brief. Never claim that a reservation was made unless a
future explicitly authorized mutation tool reports success.
"""


def create_evidence_tool(organizer_id: str):
    """Create an organizer-scoped read-only survey evidence tool."""
    from strands import tool

    @tool
    def survey_get_evidence(survey_id: str) -> str:
        """Retrieve the deterministic survey summary when reservation context is unclear."""
        return json.dumps(get_survey_evidence(survey_id, organizer_id))

    return survey_get_evidence


def create_state_tool(state: AgentState):
    """Expose operational state so the model can recover from ambiguity."""
    from strands import tool

    @tool
    def agent_get_state() -> str:
        """Read current phase, browser page, blockers, and available next actions."""
        return json.dumps(state.snapshot(), default=str)

    return agent_get_state


def create_agent(browser_tools: list[object] | None = None,
                 evidence_tool=None, state_tool=None,
                 trace: AgentTrace | None = None) -> Agent:
    """Build the agent from Google Places and local browser tools."""
    model = BedrockModel(
        model_id=settings.model_id,
        region_name=settings.aws_region,
        temperature=0.2,
    )
    agent = Agent(
        model=model,
        system_prompt=SYSTEM_PROMPT,
        callback_handler=None,
        # Context overflow must fail fast into the API's seeded fallback. The
        # default manager recursively retries overflow after trimming, which
        # is unsafe when one tool result is still too large.
        conversation_manager=NullConversationManager(),
        tools=[google_places_search, google_places_details,
               *(browser_tools or []), *( [evidence_tool] if evidence_tool else []),
               *( [state_tool] if state_tool else [])],
    )
    if trace:
        trace.attach(agent)
    return agent


def configuration_status() -> dict[str, str | bool]:
    """Report readiness without printing credentials or contacting AWS."""
    credentials = boto3.Session().get_credentials()
    return {
        "model_id": settings.model_id,
        "aws_region": settings.aws_region,
        "aws_credentials_resolved": credentials is not None,
        "google_places_configured": bool(settings.google_places_api_key),
    }


def run(prompt: str, *, user_id: str = "local-organizer", state: AgentState | None = None,
        progress_callback=None) -> str:
    """Run one organizer prompt with local browser reservation handoffs."""
    logger.info("agent stage=run_start user_id=%s prompt_chars=%d", user_id, len(prompt))
    estimated_prompt_tokens = (len(SYSTEM_PROMPT) + len(prompt) + 3) // 4
    logger.info(
        "agent stage=token_baseline system_prompt_chars=%d prompt_chars=%d "
        "estimated_initial_input_tokens=%d",
        len(SYSTEM_PROMPT), len(prompt), estimated_prompt_tokens,
    )
    state = state or AgentState()
    browser, browser_tools = create_reservation_browser_tools(user_id, state)
    trace = AgentTrace(user_id, on_phase=progress_callback)
    agent_error: Exception | None = None
    try:
        logger.info(
            "agent stage=agent_start tools=%d trace=%s",
            len(browser_tools) + 4,
            bool(os.getenv("GROUP_RESERVATIONS_TRACE") or os.getenv("GROUP_RESERVATIONS_DEBUG")),
        )
        result = str(create_agent(
            browser_tools, create_evidence_tool(user_id), create_state_tool(state), trace
        )(prompt))
        stats = trace.token_stats
        # The model provider resends conversation history on each turn, so
        # this is a lower bound rather than a billing-grade total.
        estimated_input = estimated_prompt_tokens + int(stats["estimated_tool_context_tokens"])
        estimated_output = int(stats["estimated_agent_output_tokens"])
        estimated_cost = (estimated_input * 3 + estimated_output * 15) / 1_000_000
        logger.info(
            "agent stage=token_telemetry tool_calls=%d model_calls=%d "
            "estimated_input_tokens=%d estimated_output_tokens=%d "
            "projected_input_tokens=%d cumulative_history_tokens=%d "
            "actual_input_tokens=%d actual_output_tokens=%d actual_total_tokens=%d "
            "cache_read_input_tokens=%d cache_write_input_tokens=%d "
            "tool_result_chars=%d state_snapshot_chars=%d dom_chars=%d ax_chars=%d "
            "action_trace_chars=%d image_bytes=%d largest_tool_context_tokens=%d estimated_cost_usd=%.6f",
            stats["tool_calls"], stats["model_calls"], estimated_input, estimated_output,
            stats["projected_input_tokens"], stats["cumulative_history_tokens"],
            stats["actual_input_tokens"], stats["actual_output_tokens"],
            stats["actual_total_tokens"], stats["cache_read_input_tokens"],
            stats["cache_write_input_tokens"], stats["tool_result_chars"],
            stats["state_snapshot_chars"], stats["dom_chars"], stats["ax_chars"],
            stats["action_trace_chars"], stats["image_bytes"],
            stats["largest_tool_context_tokens"], estimated_cost,
        )
        completed_handoffs = [handoff for handoff in state.reservation_handoffs.values()
                              if handoff.get("availability_verified") or handoff.get("interactive_complete")]
        abandoned_handoffs = [handoff for handoff in state.reservation_handoffs.values()
                              if handoff.get("abandoned") or handoff.get("status") == "abandoned"]
        resolved_handoffs = completed_handoffs + abandoned_handoffs
        logger.info(
            "agent stage=handoff_gate total=%d completed=%d ledger=%s",
            len(state.reservation_handoffs), len(completed_handoffs),
            json.dumps([
                {
                    "place_id": place_id,
                    "restaurant_name": handoff.get("restaurant_name"),
                    "candidate_id": handoff.get("candidate_id"),
                    "provider": handoff.get("provider"),
                    "url_prefilled": bool(handoff.get("url_prefilled") or handoff.get("prefilled")),
                    "availability_verified": bool(handoff.get("availability_verified")),
                    "interactive_complete": bool(handoff.get("interactive_complete")),
                    "abandoned": bool(handoff.get("abandoned") or handoff.get("status") == "abandoned"),
                    "last_action": handoff.get("last_action"),
                    "failure": handoff.get("failure"),
                }
                for place_id, handoff in state.reservation_handoffs.items()
            ], separators=(",", ":"), default=str),
        )
        required_handoffs = 3
        if len(state.reservation_handoffs) < required_handoffs or len(resolved_handoffs) < required_handoffs:
            missing = [handoff.get("restaurant_name", place_id)
                       for place_id, handoff in state.reservation_handoffs.items()
                       if not (handoff.get("availability_verified")
                               or handoff.get("interactive_complete")
                               or handoff.get("abandoned")
                               or handoff.get("status") == "abandoned")]
            state.phase = "recommendation_blocked"
            state.status = "failed"
            blocker = (
                "Completion gate: fewer than three completed reservation handoffs "
                f"({len(completed_handoffs)} complete, {len(abandoned_handoffs)} abandoned, "
                f"{len(resolved_handoffs)}/{required_handoffs} resolved)."
            )
            if missing:
                blocker += " Incomplete: " + ", ".join(str(name) for name in missing) + "."
            logger.warning(
                "agent stage=handoff_gate_blocked missing=%s blockers=%s",
                json.dumps(missing, default=str), json.dumps(state.blockers[-5:], default=str),
            )
            state.blockers.append(blocker)
            result = (
                f"TERMINAL STATE: blocked\n\n{blocker}\n\n"
                "The model draft below is not an authoritative success report.\n\n"
                f"MODEL DRAFT:\n{result}"
            )
        logger.info(
            "agent stage=handoff_gate_complete resolved=%d required=%d blocked=%s",
            len(resolved_handoffs), required_handoffs,
            len(resolved_handoffs) < required_handoffs,
        )
        logger.info("agent stage=agent_complete result_chars=%d", len(result))
        return result
    except Exception as exc:
        agent_error = exc
        raise
    finally:
        stats = trace.token_stats
        logger.info(
            "agent stage=token_telemetry_final model_calls=%d tool_calls=%d "
            "projected_input_tokens=%d cumulative_history_tokens=%d "
            "tool_result_chars=%d state_snapshot_chars=%d dom_chars=%d ax_chars=%d "
            "action_trace_chars=%d image_bytes=%d largest_tool_context_tokens=%d "
            "actual_input_tokens=%d actual_output_tokens=%d actual_total_tokens=%d "
            "provider_error=%s",
            stats["model_calls"], stats["tool_calls"],
            stats["projected_input_tokens"], stats["cumulative_history_tokens"],
            stats["tool_result_chars"], stats["state_snapshot_chars"],
            stats["dom_chars"], stats["ax_chars"], stats["action_trace_chars"],
            stats["image_bytes"], stats["largest_tool_context_tokens"],
            stats["actual_input_tokens"], stats["actual_output_tokens"],
            stats["actual_total_tokens"],
            f"{type(agent_error).__name__}: {agent_error}"[:300] if agent_error else None,
        )
        logger.info("agent stage=run_cleanup")
        browser.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Where Are We Eating agent")
    parser.add_argument("prompt", nargs="?", help="Organizer request for the agent")
    parser.add_argument("--user-id", default="local-organizer")
    parser.add_argument(
        "--check-config", action="store_true", help="show provider readiness"
    )
    args = parser.parse_args(argv)

    if args.check_config:
        for key, value in configuration_status().items():
            print(f"{key}={value}")
        return 0

    prompt = args.prompt or (
        "Find three restaurants in San Francisco for a group of 4 on a Friday "
        "evening. Check availability around 19:00 and explain the tradeoffs."
    )
    print(run(prompt, user_id=args.user_id))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
