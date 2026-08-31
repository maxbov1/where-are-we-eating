"""HTTP boundary for the local POC and a future AgentCore entrypoint."""

from __future__ import annotations

import json
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .opentable_mcp import run
from .auth import verify_access_token
from .database import aggregate_survey, append_response, create_survey, create_user, get_survey, init_db
from .config import settings

app = FastAPI(title="Where Are We Eating? Agent API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:4173", "http://127.0.0.1:4173"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)
init_db()


class GuestResponse(BaseModel):
    dates: list[str] = Field(min_length=1, max_length=3)
    times: list[str] = Field(min_length=1, max_length=3)
    cuisines: list[str] = Field(default_factory=list, max_length=2)
    dietary: list[str] = Field(default_factory=list, max_length=10)
    distance: str | None = None
    vibe: str | None = None
    price: str | None = None


class RecommendationRequest(BaseModel):
    survey_id: str | None = None
    event_name: str = Field(min_length=1, max_length=120)
    location: str = Field(min_length=1, max_length=160)
    dates: list[str] = Field(min_length=1, max_length=3)
    times: list[str] = Field(min_length=1, max_length=3)
    responses: list[GuestResponse] = Field(max_length=500)
    questions: dict[str, list[str]] = Field(default_factory=dict)
    report: dict[str, object] = Field(default_factory=dict)


class UserRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    cognito_sub: str | None = None


class SurveyRequest(BaseModel):
    organizer_id: str
    event_name: str = Field(min_length=1, max_length=120)
    location: str = Field(min_length=1, max_length=160)
    dates: list[str] = Field(min_length=1, max_length=3)
    times: list[str] = Field(min_length=1, max_length=3)
    questions: dict[str, list[str]] = Field(default_factory=dict)


class SurveyResponseRequest(BaseModel):
    respondent_token: str = Field(min_length=8, max_length=120)
    dates: list[str] = Field(min_length=1, max_length=3)
    times: list[str] = Field(min_length=1, max_length=3)
    cuisines: list[str] = Field(default_factory=list, max_length=2)
    dietary: list[str] = Field(default_factory=list, max_length=10)
    distance: str | None = None
    vibe: str | None = Field(default=None, min_length=1, max_length=40)
    price: str | None = Field(default=None, min_length=1, max_length=100)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/users")
def users(
    payload: UserRequest,
    x_cognito_sub: Annotated[str | None, Header()] = None,
) -> dict[str, str | bool]:
    """Create or retrieve an organizer keyed by the Cognito subject."""
    # In production this header is populated by the API Gateway JWT authorizer
    # or trusted adapter, never by the browser. The body field remains useful
    # for local development without Cognito.
    user = create_user(payload.email, x_cognito_sub or payload.cognito_sub)
    return {"id": user["id"], "email": payload.email, "is_temporary": False}


@app.post("/api/surveys")
def surveys(payload: SurveyRequest) -> dict[str, object]:
    """Persist an organizer survey and return its public token."""
    survey = create_survey(**payload.model_dump())
    share_url = f"{settings.public_app_url.rstrip('/')}/?survey={survey['public_token']}"
    return {
        "id": survey["id"],
        "public_token": survey["public_token"],
        "share_url": share_url,
        "survey": survey,
    }


@app.get("/api/surveys/{public_token}")
def survey(public_token: str) -> dict[str, object]:
    """Return public survey questions without exposing response contents."""
    record = get_survey(public_token)
    if not record:
        raise HTTPException(status_code=404, detail="Survey not found")
    return {key: record[key] for key in ("id", "public_token", "event_name", "location", "dates", "times", "questions")}

# records a guest's response to the survey
@app.post("/api/surveys/{public_token}/responses")
def survey_response(public_token: str, payload: SurveyResponseRequest) -> dict[str, object]:
    """Record a response using a hashed anonymous guest token."""
    try:
        response = append_response(
            public_token,
            guest_token=payload.respondent_token,
            dates=payload.dates, times=payload.times,
            answers={
                "cuisine": payload.cuisines,
                "dietary": payload.dietary,
                "distance": [payload.distance] if payload.distance else [],
                "vibe": [payload.vibe] if payload.vibe else [],
                "price": [payload.price] if payload.price else [],
            },
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "ok", "response": response}


@app.get("/api/surveys/{survey_id}/aggregate")
def survey_aggregate(survey_id: str) -> dict[str, object]:
    """Return cleaned, vote-counted context for the recommendation agent."""
    result = aggregate_survey(survey_id)
    if not result:
        raise HTTPException(status_code=404, detail="Survey not found")
    return result


def _agent_prompt(payload: RecommendationRequest) -> str:
    report = payload.report or {
        "event": {"name": payload.event_name, "location": payload.location},
        "response_count": len(payload.responses),
        "active_questions": list(payload.questions),
        "responses": [response.model_dump(exclude_none=True) for response in payload.responses],
    }
    return f"""Select the best restaurant options for this group event.

Authoritative cleaned group report:
{json.dumps(report, indent=2)}

Treat this report as authoritative. Do not recalculate votes or use disabled
options. When the report contains ties, still choose a concrete date, time,
and restaurant. Use this tie-break order: (1) maximize the number of guests
who can attend the date/time pair using schedule.pair_leaders, (2) prefer the
pair with the strongest restaurant availability and preference fit, (3) prefer
the strongest cuisine, budget, vibe, and distance fit, and (4) use the
earliest date/time as the stable final fallback. State which rule settled the
tie. Keep unknown provider facts explicitly unknown.

If the report includes a "confidence" block, honor it. When overall.label is
"low" or "none", present the options as tentative and lead with the group's
disagreement. Surface every item in confidence.notes (for example a split on
budget or dates) instead of papering over it. Higher confidence means you may
state a group preference more directly.

Use Google Places first and select one best restaurant, date, and time for the
group. The agent owns this decision and must not ask the organizer to choose
among tied dates or times. Then check availability for the strongest candidate.
Keep two alternatives as short fallback options, but do not present them as an
undecided top-three list. Keep the Google restaurant results even if
availability fails. Explain which date, time, and preference signals drove the
decision. Do not book anything until the organizer confirms.
For the primary restaurant, preserve exact provider URLs in separate labeled fields:
Google Maps, restaurant website, and the generic booking link plus its
provider. Prefer the restaurant website's explicit booking link; OpenTable is
only one possible provider. Never fabricate a provider URL. A listing URL does
not prove availability; report those as separate facts.
Mention up to two alternatives briefly after the primary choice. End with this
confirmation request using the selected values and prepared booking URL:
"Confirm reservation for your group of X at Y on DATE at TIME? [Confirm
reservation](URL)". The link is a human confirmation handoff; never imply the
reservation is already made.
    """


def _resolve_organizer_id(
    authorization: str | None,
    legacy_id: str | None,
) -> str:
    """Use a bearer token when supplied; retain the local header workflow."""
    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.casefold() != "bearer" or not token:
            raise HTTPException(status_code=401, detail="Invalid bearer authorization")
        try:
            return verify_access_token(token)
        except Exception as exc:
            raise HTTPException(status_code=401, detail="Invalid access token") from exc
    return legacy_id or "local-organizer"


def _payload_from_aggregate(result: dict[str, object]) -> RecommendationRequest:
    responses = result.get("responses", [])
    return RecommendationRequest(
        survey_id=str(result["survey_id"]),
        event_name=str(result["event_name"]), location=str(result["location"]),
        dates=list(result["dates"]), times=list(result["times"]),
        questions=dict(result["questions"]),
        report=dict(result.get("report", {})),
        responses=[GuestResponse(
            dates=response.get("dates", []), times=response.get("times", []),
            cuisines=response.get("cuisine", []), dietary=response.get("dietary", []),
            distance=(response.get("distance") or [None])[0],
            vibe=(response.get("vibe") or [None])[0], price=(response.get("price") or [None])[0],
        ) for response in responses],
    )


@app.post("/api/recommendations")
def recommendations(
    payload: RecommendationRequest,
    x_organizer_id: Annotated[str | None, Header()] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, str]:
    """Pass structured survey results to the agent for recommendation."""
    organizer_id = _resolve_organizer_id(authorization, x_organizer_id)
    if payload.survey_id:
        survey_record = get_survey(payload.survey_id)
        if not survey_record:
            raise HTTPException(status_code=404, detail="Survey not found")
        aggregate = aggregate_survey(payload.survey_id)
        if not aggregate:
            raise HTTPException(status_code=404, detail="Survey not found")
        payload = _payload_from_aggregate(aggregate)
    return {"status": "ok", "answer": run(_agent_prompt(payload), user_id=organizer_id)}


@app.post("/api/surveys/{survey_id}/recommendations")
def survey_recommendations(
    survey_id: str,
    x_organizer_id: Annotated[str | None, Header()] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, str]:
    """Load a persisted survey and send its responses to the agent."""
    record = get_survey(survey_id)
    if not record:
        raise HTTPException(status_code=404, detail="Survey not found")
    aggregate = aggregate_survey(survey_id)
    if not aggregate:
        raise HTTPException(status_code=404, detail="Survey not found")
    payload = _payload_from_aggregate(aggregate)
    return recommendations(
        payload, x_organizer_id=x_organizer_id, authorization=authorization
    )
