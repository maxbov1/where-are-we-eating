"""Local Strands agent wired to the OpenTable MCP server.

The POC intentionally exposes read-only OpenTable tools to the agent. Booking
and cancellation are separate actions and should require an explicit organizer
confirmation flow before they are added.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

import boto3
from mcp import StdioServerParameters, stdio_client
from strands import Agent
from strands.models import BedrockModel
from strands.tools.mcp import MCPClient

from .auth import validate_user_id
from .booking import prepare_booking_link
from .config import settings
from .places_tools import google_places_details, google_places_search
from .reservation_browser import create_reservation_browser_tools

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO if os.getenv("GROUP_RESERVATIONS_DEBUG") else logging.WARNING,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

OPENTABLE_PACKAGE = "@striderlabs/mcp-opentable"

# Google Places owns discovery and restaurant identity. OpenTable resolves
# records, checks availability, and handles the explicit organizer booking flow.
OPENTABLE_TOOLS = [
    "opentable_status",
    "opentable_login",
    "opentable_search",
    "opentable_get_restaurant",
    "opentable_check_availability",
    "opentable_make_reservation",
]

SYSTEM_PROMPT = f"""You are the restaurant evidence agent for Where Are We Eating?

Today's date is {datetime.now().astimezone().date().isoformat()}. Resolve
relative dates such as "this Friday" from today's date, and state the resolved
calendar date before checking availability.

You help an organizer turn structured group preferences into realistic
restaurant options. Always begin discovery with google_places_search. It
returns hydrated restaurant structs from Google Places, including the
canonical place ID, address, rating, price level, Google Maps link, and
opening-hour evidence. Use google_places_details only to refresh one
restaurant. Google Places is the source of truth for candidate discovery and
place identity.

For a restaurant-owned booking page, use the reservation browser tools only
with the exact booking URL returned by Google Places or the restaurant. Open
the page, call `reservation_find_booking_links`, and inspect its controls. That
tool checks anchors, iframes, and embedded provider attributes such as
`data-ot-restref`, which may contain the real OpenTable ID on a submit input.
Use the returned exact link; never derive an ID from the restaurant name.
Then fill the requested date, time, party size, and organizer details when the
page supports them. You may click search or availability controls, but never click a final Book,
Reserve, or Confirm button; return the prepared state and require an explicit
organizer confirmation for that external side effect.

After Google discovery, use an exact OpenTable booking URL when the hydrated
restaurant struct already has one. A URL shaped like
`/booking/restref/availability?restref=...` is a verified provider path from
the restaurant's website: preserve its `restref`, `lang`, `ot_source`, and
`corrid` values and use it for the availability/browser handoff. Do not call
OpenTable search merely to replace that exact URL. Otherwise, use OpenTable
search/get_restaurant to resolve the matching record before checking
availability. Pass the returned OpenTable restaurant ID or profile URL to the
availability tool; do not invent an ID or URL from the restaurant name.
When an exact `/booking/restref/availability` URL is available, prefer the
reservation browser for inspection and date/time preparation so the URL is not
rewritten by an older provider tool.
Availability does not require an
OpenTable login. Never ask the organizer to log in merely to discover
restaurants or check availability. For booking, verify the
organizer's intent, call opentable_status, and if needed call opentable_login.
Never make a booking unless the organizer clearly confirmed the exact
restaurant, date, time, and party size. Never claim a reservation was made
unless opentable_make_reservation reports success.

Before your final response, call reservation_close when you used the adaptive
reservation browser. This closes the organizer-scoped browser session.

The Google Places results are the primary answer. Always return the hydrated
Google restaurant structs and their links, even when a later OpenTable call
fails. If availability cannot be checked, label it "unknown" and explain the
failure; never replace the restaurant results with only an error message.

If Google Places is unavailable, say so clearly instead of silently making up
restaurant candidates. If reservation evidence is missing or a tool fails,
include that uncertainty in your answer.

Return concise, structured results with restaurant name, address, matching
time/date, availability evidence, and a short reason the group may prefer it.
For every candidate, preserve and print exact URLs as separate links: Google
Maps, restaurant website, and booking link. Prefer the explicit booking_uri
and booking_provider from the hydrated restaurant struct. This may be the
restaurant's own booking page, OpenTable, Resy, Tock, or another provider.
OpenTable is only a fallback when its tool returns a verified listing or live
reservation URL. Never invent a provider URL from a restaurant name or claim
that a listing link proves a slot is available. If no booking link is known,
say "Booking link unavailable". A booking link is a user handoff; live
date/time availability remains a separate provider result.
When an OpenTable tool fails, include its exact returned error in a compact
"OpenTable diagnostics" note. Do not convert a network, timeout, navigation,
or authentication error into a generic statement that the user needs to log
in. Authentication is required for booking, not for search or availability.

For each final candidate, produce a concrete booking option. First inspect the
candidate's exact Google Maps URL with the reservation browser and call
`reservation_find_booking_links`; prefer an exact Google Reserve URL when one
is present because it can provide a provider-neutral, login-free reservation
flow. Then use the exact hydrated booking URI or embedded provider URL. Never
write only "visit the website" when an exact URL or browser-prepared path is
available. Select one primary restaurant/date/time for the group. Mention up
to two alternatives briefly, but do not ask the organizer to resolve the
decision. End by asking for explicit confirmation of the selected restaurant,
date, time, and party size, with the exact prepared booking URL as the
confirmation link.

For every candidate, call `reservation_discover_booking` with its exact
restaurant website URL and the selected date, time, and party size. Use the
returned `booking_url` and `provider` in the report. The tool searches links,
forms, iframes, buttons, and data attributes; it prefers Google Reserve,
constructs a prefilled OpenTable URL from a discovered verified ID, and falls
back to the exact restaurant page when no provider link exists.
Before printing any candidate's booking link, call `prepare_booking_link` with
the candidate's raw booking URI and the selected date, time, and party size.
Print the tool's returned URL, never the raw `booking_uri`. This is required
even when the raw URI is an OpenTable `/restref/client` link.
"""


def _user_home(user_id: str) -> Path:
    """Return an isolated HOME so the MCP cookie file is user-specific."""
    validate_user_id(user_id)
    root = Path(os.getenv("GROUP_RESERVATIONS_SESSION_ROOT", ".local/opentable-sessions"))
    home = (root / user_id / "home").resolve()
    home.mkdir(parents=True, exist_ok=True)
    return home


def _playwright_browsers_path() -> str:
    """Keep browser binaries visible while cookies remain isolated per user."""
    configured = os.getenv("GROUP_RESERVATIONS_PLAYWRIGHT_BROWSERS_PATH")
    if configured:
        return configured
    if sys.platform == "darwin":
        return str(Path.home() / "Library/Caches/ms-playwright")
    return str(Path.home() / ".cache/ms-playwright")


def create_opentable_client(user_id: str) -> MCPClient:
    """Launch one MCP process with a cookie namespace for one organizer."""
    user_home = _user_home(user_id)
    parameters = StdioServerParameters(
        command="npx",
        args=["-y", OPENTABLE_PACKAGE],
        # Pass only the runtime values this local child needs. In particular,
        # do not forward unrelated shell secrets to the MCP server.
        env={
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(user_home),
            "OPENTABLE_LOCATION": settings.opentable_location,
            "PLAYWRIGHT_BROWSERS_PATH": _playwright_browsers_path(),
        },
    )
    debug = os.getenv("GROUP_RESERVATIONS_OPENTABLE_DEBUG")
    if debug:
        parameters.env["DEBUG"] = debug
    return MCPClient(
        lambda: stdio_client(parameters),
        tool_filters={"allowed": OPENTABLE_TOOLS},
    )


def create_agent(client: MCPClient, browser_tools: list[object] | None = None) -> Agent:
    """Build the Strands agent from the already configured MCP client."""
    model = BedrockModel(
        model_id=settings.model_id,
        region_name=settings.aws_region,
        temperature=0.2,
    )
    return Agent(
        model=model,
        system_prompt=SYSTEM_PROMPT,
        callback_handler=None,
        tools=[google_places_search, google_places_details, prepare_booking_link,
               *(browser_tools or []), client],
    )


def configuration_status() -> dict[str, str | bool]:
    """Report readiness without printing credentials or contacting AWS."""
    credentials = boto3.Session().get_credentials()
    return {
        "model_id": settings.model_id,
        "aws_region": settings.aws_region,
        "aws_credentials_resolved": credentials is not None,
        "google_places_configured": bool(settings.google_places_api_key),
        "opentable_location": settings.opentable_location,
        "opentable_login_mode": "booking_confirmation_only",
    }


def list_tools(user_id: str = "local-organizer") -> list[str]:
    """Start the server and return the read-only tools it actually advertises."""
    client = create_opentable_client(user_id)
    with client:
        return [tool.tool_name for tool in client.list_tools_sync()]


def run(prompt: str, *, user_id: str = "local-organizer") -> str:
    """Run one organizer prompt while keeping the MCP lifecycle bounded."""
    logger.info("agent stage=run_start user_id=%s prompt_chars=%d", user_id, len(prompt))
    client = create_opentable_client(user_id)
    browser, browser_tools = create_reservation_browser_tools(user_id)
    # Agent owns the ToolProvider lifecycle here. Starting `client` with a
    # context manager first would make Agent try to start the same MCP session
    # twice and raise "the client session is currently running".
    try:
        logger.info("agent stage=agent_start tools=%d", len(browser_tools) + 3)
        result = str(create_agent(client, browser_tools)(prompt))
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
        "--list-tools", action="store_true", help="discover read-only MCP tools"
    )
    parser.add_argument(
        "--check-config", action="store_true", help="show provider readiness"
    )
    args = parser.parse_args(argv)

    if args.list_tools:
        for name in list_tools(args.user_id):
            print(name)
        return 0

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
