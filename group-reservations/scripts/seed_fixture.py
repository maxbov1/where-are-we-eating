"""Seed a repeatable local survey for the recommendation flow."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from groupreservations.database import aggregate_survey, append_response, create_survey, create_user


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixture", nargs="?", default="tests/fixtures/san-clemente-dinner.json")
    args = parser.parse_args()
    fixture = json.loads(Path(args.fixture).read_text())
    organizer = create_user(fixture["organizer"]["email"], fixture["organizer"].get("cognito_sub"))
    survey = create_survey(organizer["id"], **fixture["survey"])
    for response in fixture["responses"]:
    answers = {key: value for key, value in response.items() if key not in {"respondent_token", "dates", "times", "availability"}}
    append_response(survey["public_token"], response["respondent_token"], response["dates"], response["times"], answers, availability=response.get("availability"))
    aggregate = aggregate_survey(survey["id"])
    print(json.dumps({"survey_id": survey["id"], "public_token": survey["public_token"], "response_count": aggregate["response_count"], "aggregate": aggregate["preference_summary"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
