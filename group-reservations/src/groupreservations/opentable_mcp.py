"""Local Strands agent wired to the OpenTable MCP server.

The POC intentionally exposes read-only OpenTable tools to the agent. Booking
and cancellation are separate actions and should require an explicit organizer
confirmation flow before they are added.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

import boto3
from mcp import StdioServerParameters, stdio_client
from strands import Agent
from strands.models import BedrockModel
from strands.tools.mcp import MCPClient

from .auth import validate_user_id
from .config import settings
from .places_tools import google_places_details, google_places_search

OPENTABLE_PACKAGE = "@striderlabs/mcp-opentable"

# Google Places owns discovery and restaurant identity. OpenTable is limited to
# availability and the explicit organizer booking flow.
OPENTABLE_TOOLS = [
    "opentable_status",
    "opentable_login",
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

After Google discovery, use OpenTable only to check availability for the
selected Google Place or to complete an explicit booking request. Availability
does not require an OpenTable login. Never ask the organizer to log in merely
to discover restaurants or check availability. For booking, verify the
organizer's intent, call opentable_status, and if needed call opentable_login.
Never make a booking unless the organizer clearly confirmed the exact
restaurant, date, time, and party size. Never claim a reservation was made
unless opentable_make_reservation reports success.

The Google Places results are the primary answer. Always return the hydrated
Google restaurant structs and their links, even when a later OpenTable call
fails. If availability cannot be checked, label it "unknown" and explain the
failure; never replace the restaurant results with only an error message.

If Google Places is unavailable, say so clearly instead of silently making up
restaurant candidates. If reservation evidence is missing or a tool fails,
include that uncertainty in your answer.

Return concise, structured results with restaurant name, address, matching
time/date, availability evidence, source link when available, and a short
reason the group may prefer it.
"""


def _user_home(user_id: str) -> Path:
    """Return an isolated HOME so the MCP cookie file is user-specific."""
    validate_user_id(user_id)
    root = Path(os.getenv("GROUP_RESERVATIONS_SESSION_ROOT", ".local/opentable-sessions"))
    home = (root / user_id / "home").resolve()
    home.mkdir(parents=True, exist_ok=True)
    return home


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
        },
    )
    return MCPClient(
        lambda: stdio_client(parameters),
        tool_filters={"allowed": OPENTABLE_TOOLS},
    )


def create_agent(client: MCPClient) -> Agent:
    """Build the Strands agent from the already configured MCP client."""
    model = BedrockModel(
        model_id=settings.model_id,
        region_name=settings.aws_region,
        temperature=0.2,
    )
    return Agent(
        model=model,
        system_prompt=SYSTEM_PROMPT,
        tools=[google_places_search, google_places_details, client],
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
    client = create_opentable_client(user_id)
    # Agent owns the ToolProvider lifecycle here. Starting `client` with a
    # context manager first would make Agent try to start the same MCP session
    # twice and raise "the client session is currently running".
    return str(create_agent(client)(prompt))


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
