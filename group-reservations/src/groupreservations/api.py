"""FastAPI boundary for the web app and deployed AgentCore runtime."""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from pydantic import BaseModel, Field, field_validator

from .agentcore_client import invoke_agentcore
from .auth import verify_access_token
from .agent_state import AgentState
from .adapters.google_places import autocomplete_locations, get_location_details
from .database import (
    SurveyClosed,
    aggregate_survey,
    append_response,
    create_survey,
    create_user,
    delete_survey,
    get_survey,
    list_surveys_for_organizer,
    init_db,
    update_survey,
)
from .config import settings
from .recommendation_response import parse_recommendation_answer

logger = logging.getLogger(__name__)

# Browser-backed reservation phases need more time than discovery and prompt
# assembly. These are elapsed-from-run-start limits, not unbounded retries.
_RECOMMENDATION_STAGE_DEADLINES = {
    "agent_reasoning": 300,
    "restaurant_discovery": 180,
    "restaurant_hydration": 240,
    "reservation_scan": 240,
    "reservation_inspection": 300,
    "reservation_preparation": 360,
    "reservation_availability": 420,
    "cleanup": 480,
}

class RateLimitMiddleware(BaseHTTPMiddleware):
    """Small process-local limiter for the POC and a safe default at the edge."""

    def __init__(self, app: FastAPI, limit: int, window_seconds: int = 60) -> None:
        super().__init__(app)
        self.limit = max(1, limit)
        self.window_seconds = window_seconds
        self._requests: defaultdict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    async def dispatch(self, request: Request, call_next):
        if request.url.path == "/health" or not request.url.path.startswith("/api/"):
            return await call_next(request)
        client = request.client.host if request.client else "unknown"
        path = request.url.path
        limit = self.limit
        if request.method == "POST" and path.endswith("/recommendations"):
            limit = min(limit, 10)
        elif request.method == "POST" and path.endswith("/responses"):
            limit = min(limit, 30)
        elif request.method == "POST" and path in {"/api/users", "/api/surveys"}:
            limit = min(limit, 20)
        key = f"{client}:{request.method}:{path}"
        now = time.monotonic()
        with self._lock:
            bucket = self._requests[key]
            while bucket and bucket[0] <= now - self.window_seconds:
                bucket.popleft()
            if len(bucket) >= limit:
                retry_after = max(1, int(self.window_seconds - (now - bucket[0])))
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Too many requests. Please try again shortly."},
                    headers={"Retry-After": str(retry_after)},
                )
            bucket.append(now)
        return await call_next(request)


app = FastAPI(title="Where Are We Eating? Agent API", version="0.1.0")
allowed_origins = [origin.strip() for origin in settings.cors_allowed_origins.split(",") if origin.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_origin_regex=r"https://([a-z0-9-]+\.)?vercel\.app",
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)
app.add_middleware(RateLimitMiddleware, limit=settings.rate_limit_requests_per_minute)
init_db()

_recommendation_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="recommendation")
_recommendation_runs: dict[str, dict[str, object]] = {}
_recommendation_runs_lock = threading.Lock()
_DEMO_FALLBACK_ANSWER = """{
  "status": "blocked",
  "group_fit": "Live research was unavailable, so these seeded options are tentative. Review the links directly before choosing a restaurant.",
  "primary": {
    "name": "Roka Akor San Francisco",
    "description": "Japanese robata, sushi, and Wagyu in Jackson Square.",
    "tradeoff": "A strong fit for a special group dinner.",
    "traits": ["Japanese", "Special occasion", "Group-friendly"],
    "availability": {"status": "unknown", "summary": "Live availability was not verified in fallback mode."},
    "restaurant_url": "https://www.rokaakor.com/san-francisco/",
    "reservation": {"status": "unknown", "url": "https://www.opentable.com/r/roka-akor-san-francisco", "provider": "OpenTable", "label": "Open Roka Akor reservation options"}
  },
  "alternatives": [
    {
      "name": "Ozumo San Francisco",
      "description": "Contemporary Japanese sushi and robata near the Embarcadero.",
      "traits": ["Japanese", "Waterfront", "Group-friendly"],
      "tradeoff": "A good waterfront group option.",
      "restaurant_url": "https://www.ozumosanfrancisco.com/",
      "availability": {"status": "unknown", "summary": "Live availability was not verified in fallback mode."},
      "reservation": {"status": "unknown", "url": "https://www.ozumosanfrancisco.com/", "provider": "Restaurant website", "label": "Open Ozumo reservation options"}
    },
    {
      "name": "Akari Japanese Bistro",
      "description": "An intimate Japanese bistro with a more relaxed feel.",
      "traits": ["Japanese", "Intimate", "Relaxed"],
      "tradeoff": "Best for a quieter, lower-key dinner.",
      "restaurant_url": "https://www.akari-japanese.com/",
      "availability": {"status": "unknown", "summary": "Live availability was not verified in fallback mode."},
      "reservation": {"status": "unknown", "url": "https://www.akari-japanese.com/reservation", "provider": "Restaurant website", "label": "Open Akari reservation options"}
    }
  ],
  "blocker": {"code": "LIVE_RESEARCH_UNAVAILABLE", "title": "I can't complete further than this", "explanation": "Live reservation verification was unavailable, so no availability or reservation is being claimed.", "next_step": "Open a restaurant link and confirm availability directly."},
  "next_steps": ["Confirm the date, time, and party size directly with the restaurant."]
}"""


def _set_recommendation_run(run_id: str, **changes: object) -> dict[str, object] | None:
    with _recommendation_runs_lock:
        record = _recommendation_runs.get(run_id)
        if record is not None:
            record.update(changes)
            return dict(record)
    return None


def _fallback_result(run_id: str, reason: str) -> None:
    _set_recommendation_run(
        run_id,
        status="fallback",
        stage="fallback_ready",
        answer=_DEMO_FALLBACK_ANSWER,
        response=parse_recommendation_answer(_DEMO_FALLBACK_ANSWER),
        fallback=True,
        fallback_reason=reason,
        error_code="CONTEXT_WINDOW_OVERFLOW" if "context window" in reason.casefold() else "AGENT_RUN_FAILED",
        completed_at=time.time(),
    )


def _is_context_overflow(error: BaseException) -> bool:
    """Recognize provider/context failures through wrapped exception causes."""
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        label = f"{type(current).__name__} {current}".casefold()
        if "contextwindowoverflow" in label or "context window overflow" in label:
            return True
        current = current.__cause__ or current.__context__
    return False


def _run_recommendation(run_id: str, prompt: str, organizer_id: str, state: AgentState) -> None:
    stage_messages = {
        "restaurant_discovery": "Finding restaurants with Google Places",
        "restaurant_hydration": "Verifying restaurant details",
        "reservation_scan": "Scanning reservation paths",
        "reservation_inspection": "Inspecting booking controls",
        "reservation_availability": "Checking live availability",
        "agent_reasoning": "Choosing the best fit for the group",
    }

    def on_phase(phase: str, _tool: str) -> None:
        now = time.time()
        _set_recommendation_run(
            run_id, status="running", stage=phase,
            message=stage_messages.get(phase, "Researching the best options"),
            last_tool=_tool,
            last_progress_at=now,
        )

    on_phase("agent_reasoning", "start")
    try:
        answer = invoke_agentcore(prompt, user_id=organizer_id)
    except Exception as exc:
        record = _set_recommendation_run(run_id, last_error_type=type(exc).__name__)
        logger.exception(
            "recommendation background run failed run_id=%s stage=%s elapsed_seconds=%.1f",
            run_id,
            (record or {}).get("stage", "unknown"),
            time.time() - float((record or {}).get("created_at", time.time())),
        )
        if _is_context_overflow(exc):
            logger.warning("recommendation context overflow; returning fallback run_id=%s", run_id)
            _fallback_result(run_id, "Bedrock context window overflow; live research was stopped before retrying.")
        else:
            _fallback_result(run_id, f"Live recommendation failed: {type(exc).__name__}")
        return
    with _recommendation_runs_lock:
        record = _recommendation_runs.get(run_id)
        if not record or record.get("status") == "timeout":
            logger.warning(
                "recommendation result discarded after watchdog run_id=%s status=%s elapsed_seconds=%.1f",
                run_id,
                (record or {}).get("status", "missing"),
                time.time() - float((record or {}).get("created_at", time.time())),
            )
            return
        record.update({
            "status": "complete",
            "stage": "recommendation_ready",
            "message": "The recommendation and reservation evidence are ready.",
            "answer": answer,
            "response": parse_recommendation_answer(answer),
            "fallback": False,
            "completed_at": time.time(),
        })
        logger.info(
            "recommendation background run complete run_id=%s elapsed_seconds=%.1f",
            run_id, time.time() - float(record.get("created_at", time.time())),
        )


def _expire_recommendation(run_id: str) -> None:
    extend_cleanup = False
    reschedule_for: float | None = None
    with _recommendation_runs_lock:
        record = _recommendation_runs.get(run_id)
        if not record or record.get("status") not in {"queued", "running"}:
            return
        elapsed = time.time() - float(record.get("created_at", time.time()))
        stage = str(record.get("stage") or "")
        deadline = max(
            settings.recommendation_timeout_seconds,
            _RECOMMENDATION_STAGE_DEADLINES.get(stage, settings.recommendation_timeout_seconds),
        )
        if elapsed < deadline:
            reschedule_for = max(1.0, deadline - elapsed)
            record.update({
                "message": f"Still working through {stage.replace('_', ' ')}.",
                "watchdog_deadline_seconds": deadline,
            })
        # The agent has already produced its answer by this point; browser
        # shutdown is still running in the agent's finally block. Give cleanup
        # one bounded grace period instead of replacing a nearly-complete run
        # with the seeded fallback at the exact watchdog deadline.
        elif stage == "cleanup" and not record.get("cleanup_grace_used"):
            record.update({
                "message": "Finishing browser cleanup before returning the recommendation.",
                "cleanup_grace_used": True,
            })
            extend_cleanup = True
        elif reschedule_for is None:
            logger.warning(
                "recommendation watchdog timeout run_id=%s stage=%s tool=%s elapsed_seconds=%.1f phase_idle_seconds=%.1f timeout_seconds=%s",
                run_id,
                record.get("stage", "unknown"),
                record.get("last_tool", "unknown"),
                elapsed,
                time.time() - float(record.get("last_progress_at", record.get("created_at", time.time()))),
                settings.recommendation_timeout_seconds,
            )
            record.update({
                "status": "timeout",
                "stage": "fallback_ready",
                "message": "Live research took too long, so the demo result is ready.",
                "fallback": True,
                "fallback_reason": "The live recommendation exceeded the local demo timeout.",
                "error_code": "RECOMMENDATION_TIMEOUT",
                "answer": _DEMO_FALLBACK_ANSWER,
                "completed_at": time.time(),
            })
    if reschedule_for is not None:
        logger.info(
            "recommendation watchdog extended run_id=%s stage=%s tool=%s elapsed_seconds=%.1f phase_idle_seconds=%.1f deadline_seconds=%.1f next_check_seconds=%.1f",
            run_id, stage, record.get("last_tool", "unknown"), elapsed,
            time.time() - float(record.get("last_progress_at", record.get("created_at", time.time()))),
            deadline, reschedule_for,
        )
        timer = threading.Timer(reschedule_for, _expire_recommendation, args=(run_id,))
        timer.daemon = True
        timer.start()
        return
    if extend_cleanup:
        logger.warning(
            "recommendation watchdog cleanup grace run_id=%s elapsed_seconds=%.1f grace_seconds=60",
            run_id, elapsed,
        )
        timer = threading.Timer(60, _expire_recommendation, args=(run_id,))
        timer.daemon = True
        timer.start()


def _enqueue_recommendation(prompt: str, organizer_id: str, state: AgentState) -> dict[str, object]:
    run_id = f"recommendation-{uuid.uuid4().hex[:16]}"
    now = time.time()
    with _recommendation_runs_lock:
        _recommendation_runs[run_id] = {
            "run_id": run_id,
            "organizer_id": organizer_id,
            "status": "queued",
            "stage": "queued",
            "message": "Your group preferences are queued for research.",
            "fallback": False,
            "created_at": now,
            "last_error_type": None,
            "last_tool": None,
            "last_progress_at": now,
            "watchdog_deadline_seconds": settings.recommendation_timeout_seconds,
        }
    _recommendation_executor.submit(_run_recommendation, run_id, prompt, organizer_id, state)
    timer = threading.Timer(settings.recommendation_timeout_seconds, _expire_recommendation, args=(run_id,))
    timer.daemon = True
    timer.start()
    return {"run_id": run_id, "status": "queued", "stage": "queued", "message": "Your group preferences are queued for research."}


class GuestResponse(BaseModel):
    dates: list[str] = Field(min_length=1, max_length=3)
    times: list[str] = Field(min_length=1, max_length=9)
    availability: dict[str, list[str]] = Field(default_factory=dict)
    cuisines: list[str] = Field(default_factory=list, max_length=2)
    dietary: list[str] = Field(default_factory=list, max_length=10)
    distance: str | None = None
    vibe: str | None = None
    price: str | None = None
    origin_place_id: str | None = None
    origin_label: str | None = None
    origin_lat: float | None = None
    origin_lng: float | None = None


class RecommendationRequest(BaseModel):
    survey_id: str | None = None
    event_name: str = Field(min_length=1, max_length=120)
    location: str = Field(min_length=1, max_length=160)
    dates: list[str] = Field(min_length=1, max_length=3)
    times: list[str] = Field(min_length=1, max_length=9)
    availability: dict[str, list[str]] = Field(default_factory=dict)
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
    times: list[str] = Field(min_length=1, max_length=9)
    availability: dict[str, list[str]] = Field(default_factory=dict)
    questions: dict[str, list[str]] = Field(default_factory=dict)
    location_place_id: str | None = Field(default=None, max_length=200)
    location_lat: float | None = Field(default=None, ge=-90, le=90)
    location_lng: float | None = Field(default=None, ge=-180, le=180)
    # Optional override; when omitted the survey closes to new responses two
    # days after creation
    expires_at: str | None = Field(default=None, max_length=40)

    @field_validator("expires_at")
    @classmethod
    def _validate_expires_at(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("expires_at must be an ISO 8601 timestamp") from exc
        return value


class SurveyUpdateRequest(BaseModel):
    event_name: str | None = Field(default=None, min_length=1, max_length=120)
    location: str | None = Field(default=None, min_length=1, max_length=160)
    dates: list[str] | None = Field(default=None, min_length=1, max_length=3)
    times: list[str] | None = Field(default=None, min_length=1, max_length=9)
    availability: dict[str, list[str]] | None = None
    questions: dict[str, list[str]] | None = None
    location_place_id: str | None = Field(default=None, max_length=200)
    location_lat: float | None = Field(default=None, ge=-90, le=90)
    location_lng: float | None = Field(default=None, ge=-180, le=180)
    expires_at: str | None = Field(default=None, max_length=40)

    @field_validator("expires_at")
    @classmethod
    def _validate_update_expires_at(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                datetime.fromisoformat(value)
            except ValueError as exc:
                raise ValueError("expires_at must be an ISO 8601 timestamp") from exc
        return value


class SurveyResponseRequest(BaseModel):
    respondent_token: str = Field(min_length=8, max_length=120)
    dates: list[str] = Field(min_length=1, max_length=3)
    times: list[str] = Field(min_length=1, max_length=9)
    availability: dict[str, list[str]] = Field(default_factory=dict)
    cuisines: list[str] = Field(default_factory=list, max_length=2)
    dietary: list[str] = Field(default_factory=list, max_length=10)
    distance: str | None = None
    vibe: str | None = Field(default=None, min_length=1, max_length=40)
    price: str | None = Field(default=None, min_length=1, max_length=100)
    origin_place_id: str | None = Field(default=None, max_length=200)
    origin_label: str | None = Field(default=None, min_length=2, max_length=160)
    origin_lat: float | None = Field(default=None, ge=-90, le=90)
    origin_lng: float | None = Field(default=None, ge=-180, le=180)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/locations/autocomplete")
def locations_autocomplete(
    input: str = Query(min_length=2, max_length=120),
    session_token: str | None = Query(default=None, max_length=200),
    cities_only: bool = False,
) -> dict[str, object]:
    """Proxy Google location predictions without exposing the Places key."""
    if not settings.google_places_api_key:
        raise HTTPException(status_code=503, detail="Google Places is not configured")
    try:
        return {"predictions": autocomplete_locations(settings.google_places_api_key, input, session_token=session_token, cities_only=cities_only)}
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Google Places location lookup failed") from exc


class LocationDetailsRequest(BaseModel):
    place_id: str = Field(min_length=1, max_length=200)
    session_token: str | None = Field(default=None, max_length=200)


@app.post("/api/locations/details")
def locations_details(payload: LocationDetailsRequest) -> dict[str, object]:
    """Resolve a selected prediction into a canonical label and coordinates."""
    if not settings.google_places_api_key:
        raise HTTPException(status_code=503, detail="Google Places is not configured")
    try:
        return get_location_details(settings.google_places_api_key, payload.place_id, session_token=payload.session_token)
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Google Places location details failed") from exc


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
def surveys(
    payload: SurveyRequest,
    x_organizer_id: Annotated[str | None, Header()] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, object]:
    """Persist an organizer survey and return its public token."""
    resolved = _resolve_organizer_id(authorization, x_organizer_id or payload.organizer_id)
    data = payload.model_dump()
    data["organizer_id"] = resolved
    if not data["availability"]:
        data["availability"] = {date: list(data["times"]) for date in data["dates"]}
    survey = create_survey(**data)
    share_url = f"{settings.public_app_url.rstrip('/')}/?survey={survey['public_token']}"
    return {
        "id": survey["id"],
        "public_token": survey["public_token"],
        "share_url": share_url,
        "expires_at": survey["expires_at"],
        "survey": survey,
    }


@app.patch("/api/surveys/{survey_id}")
def survey_update(
    survey_id: str,
    payload: SurveyUpdateRequest,
    x_organizer_id: Annotated[str | None, Header()] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, object]:
    organizer_id = _resolve_organizer_id(authorization, x_organizer_id)
    updated = update_survey(survey_id, organizer_id, **payload.model_dump(exclude_unset=True))
    if not updated:
        raise HTTPException(status_code=404, detail="Survey not found")
    return {"survey": updated}


@app.delete("/api/surveys/{survey_id}", status_code=204)
def survey_delete(
    survey_id: str,
    x_organizer_id: Annotated[str | None, Header()] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    organizer_id = _resolve_organizer_id(authorization, x_organizer_id)
    if not delete_survey(survey_id, organizer_id):
        raise HTTPException(status_code=404, detail="Survey not found")


@app.get("/api/surveys/{public_token}")
def survey(public_token: str) -> dict[str, object]:
    """Return public survey questions without exposing response contents."""
    record = get_survey(public_token)
    if not record:
        raise HTTPException(status_code=404, detail="Survey not found")
    return {key: record[key] for key in ("id", "public_token", "event_name", "location", "dates", "times", "availability", "questions", "expires_at", "is_open")}


@app.get("/api/organizers/{organizer_id}/surveys")
def organizer_surveys(
    organizer_id: str,
    x_organizer_id: Annotated[str | None, Header()] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, object]:
    """Return the organizer's compact active-event shelf."""
    resolved = _resolve_organizer_id(authorization, x_organizer_id)
    if resolved != organizer_id:
        raise HTTPException(status_code=404, detail="Organizer not found")
    return {"events": list_surveys_for_organizer(organizer_id)}

# records a guest's response to the survey
@app.post("/api/surveys/{public_token}/responses")
def survey_response(public_token: str, payload: SurveyResponseRequest) -> dict[str, object]:
    """Record a response using a hashed anonymous guest token."""
    try:
        response = append_response(
            public_token,
            guest_token=payload.respondent_token,
            dates=payload.dates, times=payload.times,
            availability=payload.availability,
            answers={
                "cuisine": payload.cuisines,
                "dietary": payload.dietary,
                "distance": [payload.distance] if payload.distance else [],
                "vibe": [payload.vibe] if payload.vibe else [],
                "price": [payload.price] if payload.price else [],
            },
            origin_place_id=payload.origin_place_id,
            origin_label=payload.origin_label,
            origin_lat=payload.origin_lat,
            origin_lng=payload.origin_lng,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SurveyClosed as exc:
        raise HTTPException(status_code=410, detail=str(exc)) from exc
    return {"status": "ok", "response": response}


@app.get("/api/surveys/{survey_id}/aggregate")
def survey_aggregate(
    survey_id: str,
    x_organizer_id: Annotated[str | None, Header()] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, object]:
    """Return cleaned, vote-counted context for the recommendation agent."""
    result = aggregate_survey(survey_id)
    if not result:
        raise HTTPException(status_code=404, detail="Survey not found")
    if authorization or x_organizer_id:
        organizer_id = _resolve_organizer_id(authorization, x_organizer_id)
        survey_record = get_survey(survey_id)
        if not survey_record or survey_record.get("organizer_id") != organizer_id:
            raise HTTPException(status_code=404, detail="Survey not found")
    # Guest origins are private inputs for the recommendation run, not part of
    # the public aggregate inspection endpoint.
    private_fields = {"origin_place_id", "origin_label", "origin_lat", "origin_lng"}
    result["responses"] = [{key: value for key, value in response.items() if key not in private_fields} for response in result.get("responses", [])]
    result["report"]["responses"] = [{key: value for key, value in response.items() if key not in private_fields} for response in result["report"].get("responses", [])]
    return result


def _agent_prompt(payload: RecommendationRequest) -> str:
    report = payload.report or {
        "event": {"name": payload.event_name, "location": payload.location},
        "response_count": len(payload.responses),
        "active_questions": list(payload.questions),
        "schedule": {"times_by_date": payload.availability},
        "responses": [response.model_dump(exclude_none=True) for response in payload.responses],
    }
    # The deterministic report already contains the vote counts and leaders
    # needed for ranking. Response-by-response records repeat that information
    # and grow the model context linearly with every guest.
    report = dict(report)
    report.pop("responses", None)
    response_count = report.get("response_count", len(payload.responses))
    return f"""Select the best restaurant options for this group event.

Authoritative cleaned group report:
{json.dumps(report, separators=(",", ":"), ensure_ascii=False)}

Survey evidence ID: {payload.survey_id or "unavailable"}. If any group
context is missing, contradictory, or unclear during tool calls, use the
survey_get_evidence tool with this ID before making a decision.

PARTY SIZE
This MVP has no separate party-size question. Treat the authoritative
response_count ({response_count}) as the number of guests for reservation
preparation: one submitted response means party size 1, five responses means
party size 5. Never invent a placeholder party size. If response_count is 0,
party size is unknown; do not prepare a booking URL or claim availability and
end with a request for the organizer to provide the group size.

Treat this report as authoritative. Do not recalculate votes or use disabled
options. Treat schedule.times_by_date as authoritative: a time is valid only
for the date where it is listed. Treat schedule.recommended_pairs (and its
pair_consensus) as the primary schedule recommendation; date_leaders and
time_leaders are independent tallies and must not be combined as an exact pair.
When the report contains ties, still choose a concrete date, time, and
restaurant. Use this tie-break order: (1) maximize the number of guests
who can attend the date/time pair using schedule.recommended_pairs, (2) prefer the
pair with the strongest restaurant availability and preference fit, (3) prefer
the strongest cuisine, budget, vibe, and distance fit, and (4) use the
earliest date/time as the stable final fallback. State which rule settled the
tie. Keep unknown provider facts explicitly unknown.

If the report includes a "confidence" block, honor it. When overall.label is
"low" or "none", present the options as tentative and lead with the group's
disagreement. Surface every item in confidence.notes (for example a split on
budget or dates) instead of papering over it. Higher confidence means you may
state a group preference more directly.

Use Google Places first and select one best restaurant plus exactly two
secondary fallback restaurants when at least three viable results exist. The
agent owns this decision and must not ask the organizer to choose among tied
dates or times. Hydrate all three, inspect each reservation path, and check
availability for the primary. Keep the Google restaurant results even if
availability fails. Explain which date, time, and preference signals drove the
decision. Do not book anything until the organizer confirms.
For the primary restaurant, preserve exact provider URLs in separate labeled fields:
Google Maps, restaurant website, and the generic booking link plus its
provider. Prefer the restaurant website's explicit booking link; OpenTable is
only one possible provider. Never fabricate a provider URL. A listing URL does
not prove availability; report those as separate facts.
Return ONLY valid JSON. Do not wrap it in Markdown fences and do not add prose
before or after it. Use exactly this shape:
{{
  "status": "ready" or "blocked",
  "group_fit": "1-2 concise sentences explaining why the group choices led to the primary",
  "primary": {{
    "name": "restaurant name",
    "description": "one factual sentence describing the place from verified Google Places evidence",
    "tradeoff": "one sentence about the fit or tradeoff",
    "traits": ["cuisine", "vibe", "price or dietary fact when verified"],
    "availability": {{"status":"verified" or "unknown" or "unavailable", "summary":"what was verified for the selected date/time", "checked_at":"ISO timestamp or null", "source_url":"exact evidence URL or null"}},
    "reservation": {{"status":"available" or "unknown" or "unavailable", "url":"exact observed/prepared booking URL or null", "provider":"provider name or null", "label":"Get [restaurant]'s reservation"}}
  }},
  "alternatives": [
    {{"name":"...", "description":"...", "traits":[], "tradeoff":"...", "availability":{{}}, "restaurant_url":"...", "reservation":{{}}}},
    {{"name":"...", "description":"...", "traits":[], "tradeoff":"...", "availability":{{}}, "restaurant_url":"...", "reservation":{{}}}}
  ],
  "blocker": {{"code":"...","title":"...","explanation":"...","next_step":"..."}} or null,
  "next_steps": ["one or two safe actions the organizer can take next"]
}}
The UI turns the primary and alternatives into linked restaurant cards and turns
reservation.url values into buttons. Never fabricate a URL. Never claim that a
reservation was made; a reservation URL is only a human handoff for organizer review.
If reservation verification or browser automation is unavailable, set status to
"blocked", preserve the restaurant recommendations, set availability.status to
"unknown", and explain the blocker in blocker. Do not replace
the recommendations with an error message.
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
    if settings.require_auth:
        raise HTTPException(status_code=401, detail="Bearer authentication required")
    return legacy_id or "local-organizer"


def _payload_from_aggregate(result: dict[str, object]) -> RecommendationRequest:
    responses = result.get("responses", [])
    return RecommendationRequest(
        survey_id=str(result["survey_id"]),
        event_name=str(result["event_name"]), location=str(result["location"]),
        dates=list(result["dates"]), times=list(result["times"]),
        availability=dict(result.get("availability", {})),
        questions=dict(result["questions"]),
        report=dict(result.get("report", {})),
        responses=[GuestResponse(
            dates=response.get("dates", []), times=response.get("times", []),
            availability=response.get("availability", {}),
            cuisines=response.get("cuisine", []), dietary=response.get("dietary", []),
            distance=(response.get("distance") or [None])[0],
            vibe=(response.get("vibe") or [None])[0], price=(response.get("price") or [None])[0],
            origin_place_id=response.get("origin_place_id"), origin_label=response.get("origin_label"),
            origin_lat=response.get("origin_lat"), origin_lng=response.get("origin_lng"),
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
    state = AgentState(
        survey_id=payload.survey_id,
        group_location=payload.location,
    )
    return _enqueue_recommendation(_agent_prompt(payload), organizer_id, state)


@app.get("/api/recommendations/{run_id}")
def recommendation_status(
    run_id: str,
    x_organizer_id: Annotated[str | None, Header()] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, object]:
    """Return local recommendation progress and the eventual answer."""
    organizer_id = _resolve_organizer_id(authorization, x_organizer_id)
    with _recommendation_runs_lock:
        record = _recommendation_runs.get(run_id)
        if not record:
            raise HTTPException(status_code=404, detail="Recommendation run not found")
        if record.get("organizer_id") != organizer_id:
            raise HTTPException(status_code=404, detail="Recommendation run not found")
        result = dict(record)
    result.pop("organizer_id", None)
    return result


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
    organizer_id = _resolve_organizer_id(authorization, x_organizer_id)
    if record.get("organizer_id") != organizer_id:
        raise HTTPException(status_code=404, detail="Survey not found")
    aggregate = aggregate_survey(survey_id)
    if not aggregate:
        raise HTTPException(status_code=404, detail="Survey not found")
    payload = _payload_from_aggregate(aggregate)
    return recommendations(payload, x_organizer_id=organizer_id)
