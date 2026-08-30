"""Run fixture seeding, deterministic cleaning, and the live restaurant agent."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from groupreservations.api import _agent_prompt, _payload_from_aggregate
from groupreservations.database import aggregate_survey, append_response, create_survey, create_user
from groupreservations.opentable_mcp import run


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixture", nargs="?", default="tests/fixtures/san-clemente-dinner.json")
    parser.add_argument("--aggregate-only", action="store_true", help="stop after printing the cleaned report")
    args = parser.parse_args()
    fixture = json.loads(Path(args.fixture).read_text())
    organizer = create_user(fixture["organizer"]["email"], fixture["organizer"].get("cognito_sub"))
    survey = create_survey(organizer["id"], **fixture["survey"])
    for response in fixture["responses"]:
        answers = {key: value for key, value in response.items() if key not in {"respondent_token", "dates", "times"}}
        append_response(survey["public_token"], response["respondent_token"], response["dates"], response["times"], answers)
    aggregate = aggregate_survey(survey["id"])
    print("CLEANED REPORT")
    print(json.dumps(aggregate["report"], indent=2))
    if args.aggregate_only:
        return 0
    print("\nAGENT RESULT")
    print(run(_agent_prompt(_payload_from_aggregate(aggregate)), user_id=organizer["id"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
