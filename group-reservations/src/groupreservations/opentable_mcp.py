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

CONSTRAINTS
- Google Places is the source of truth for restaurant identity and discovery.
- Use only exact URLs and candidate IDs returned by tools or Google Places.
- Every browser action must remain bound to the same candidate_id and URL. If
  verification fails, stop acting on that page and recover from the failure
  state.
- `reservation_prepare`, `reservation_fill`, and `reservation_click` require a
  successful `reservation_verify` for the same candidate_id and URL. The tools
  reject calls that do not satisfy this precondition.
- Use these signatures when needed: `reservation_prepare(candidate_id, url,
  booking_url, date, time, party_size)`, `reservation_fill(candidate_id, url,
  field, value)`, and `reservation_click(candidate_id, url, label)`.
- `booking_url` must be an exact URL returned in the scan result for the same
  verified page. There is no generic URL-preparation shortcut.
- Reservation diagnosis may continue after verification: inspect the verified
  page, fill only date/time/party-size fields, and click only a clearly
  non-final `Search`, `Find a table`, or availability control. Then inspect or
  scan the resulting page and report observed availability. This is especially
  important for embedded providers such as Toast, whose exact iframe URL must
  be opened and verified as its own candidate.
- Never submit a booking. Final reservation controls remain organizer-gated;
  stop at the first confirmation or guest-details step and ask the organizer
  whether to continue.
- Never click a final Book, Reserve, Confirm, Submit, or Complete control.
- Do not search external reservation providers or infer provider IDs from names.
- Treat missing, stale, or failed evidence as unknown; never turn it into a
  positive availability or booking claim.
- If group context is unclear, call `survey_get_evidence`. If operational
  context is unclear, call `agent_get_state`.
- Before ending after browser use, close the browser.
- A candidate may appear in the final report as a reservation option only if
  its exact page or booking candidate was scanned. Otherwise label it
  `not inspected`; do not claim that its reservation channel is unknown based
  on an unscanned page. If a scan exposes a navigation action such as
  `/general-4`, follow that exact action and scan the destination before
  reporting the candidate's reservation options.

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


def run(prompt: str, *, user_id: str = "local-organizer", state: AgentState | None = None) -> str:
    """Run one organizer prompt with local browser reservation handoffs."""
    logger.info("agent stage=run_start user_id=%s prompt_chars=%d", user_id, len(prompt))
    state = state or AgentState()
    browser, browser_tools = create_reservation_browser_tools(user_id, state)
    trace = AgentTrace(user_id)
    try:
        logger.info(
            "agent stage=agent_start tools=%d trace=%s",
            len(browser_tools) + 4,
            bool(os.getenv("GROUP_RESERVATIONS_TRACE") or os.getenv("GROUP_RESERVATIONS_DEBUG")),
        )
        result = str(create_agent(
            browser_tools, create_evidence_tool(user_id), create_state_tool(state), trace
        )(prompt))
        logger.info("agent stage=agent_complete result_chars=%d", len(result))
        return result
    finally:
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
