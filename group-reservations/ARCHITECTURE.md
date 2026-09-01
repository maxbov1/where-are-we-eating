# Group Reservations Architecture

## System context

```text
Organizer UI ──┐
Guest survey ──┼──> Application API ──> Event/session store
SMS link ──────┘              │
                              ├──> Preference aggregation
                              ├──> Restaurant search + place hydration
                              ├──> Reservation evidence
                              └──> Recommendation agent
```

The organizer owns the event. Guests own only their response. The public URL
must never grant access to organizer controls.

### Identity and third-party session isolation

```text
Authorization: Bearer <our JWT>
        -> verify signature, audience, expiry
        -> sub = organizer_id
        -> create MCP client for organizer_id
        -> child HOME = session-root/organizer_id/home
        -> OpenTable cookies stay in that user's namespace
```

The JWT identifies the user to our application; it is not an OpenTable
credential. The current community MCP server persists cookies at
`~/.strider/opentable/cookies.json`, so a shared process would create a
cross-user session leak. The POC therefore uses one short-lived MCP process per
organizer request with an isolated `HOME`. Production should replace the local
directory with encrypted per-user session storage or a per-user worker.

## Components

- `models.py`: canonical `Place` and `AvailabilityEvidence` structs. `Place`
  includes Google’s `reservable` flag, which indicates reservation support but
  not live date/time availability.
- `adapters/google_places.py`: REST calls to Google Places search and details.
- `places_tools.py`: Strands-compatible tools that search and immediately
  hydrate Google restaurant candidates.
- `opentable_mcp.py`: Bedrock-backed Strands agent and per-organizer OpenTable
  MCP process. Google Places owns discovery; OpenTable owns availability and
  booking.
- `api.py`: FastAPI HTTP boundary accepting structured survey responses and
  invoking the agent. This boundary is deliberately portable to an AgentCore
  runtime later.
- `database.py`: SQLite persistence mirroring the production schema. `users`
  stores organizers and temporary guests; `surveys`, `survey_questions`, and
  `survey_options` store the invitation; `survey_responses` and
  `response_answers` store independent guest submissions.
- `auth.py`: application JWT minting/verification and safe organizer IDs.
- `config.py`: environment-backed AWS, Google, OpenTable, and JWT settings.

The repository now contains a local web API and static survey UI. The local
SQLite schema mirrors durable production storage, while Cognito and Aurora
PostgreSQL are the production targets. SMS delivery remains a planned
application component. The local API exposes deterministic aggregation before
agent orchestration.

## Data Flow

```text
frontend survey payload
    -> POST /api/users or /api/surveys or /api/surveys/{token}/responses
    -> Google Places Autocomplete + Place Details for city/origin selection
    -> SQLite normalized survey tables (Aurora PostgreSQL in production)
    -> GET /api/surveys/{survey_id}/aggregate
    -> preference counts and cleaned response context
    -> POST /api/surveys/{survey_id}/recommendations
    -> structured request validation
    -> agent prompt
    -> google_places_search
    -> Google Places searchText
    -> get_place for every candidate
    -> hydrated Place structs + source/opening-hours evidence
    -> OpenTable availability checks for selected Place candidates
    -> ranked explanation with evidence and uncertainty
    -> explicit organizer confirmation
    -> OpenTable login + reservation booking
```

Google Place IDs are the canonical restaurant identity passed into later
availability checks. A failed OpenTable check must not erase Google results;
availability is returned as unknown with the failure recorded.

## Domain objects

- `Event`: organizer, title, status, response URL/token, survey, candidate
  dates, and candidate time slots.
- `SurveyQuestion`: stable key, prompt, answer type, options, and active state.
- `GuestResponse`: event, opaque respondent token, structured answers,
  submitted timestamp, and revision timestamp.
- `PreferenceSummary`: response count, participation rate, per-option support,
  conflicts, and confidence.
- `GuestOrigin`: optional selected Google Place ID, display label, and
  coordinates for approximate guest travel origin; exact home addresses are
  not required or stored.
- Guest budget answers use explicit per-person bands. Guest distance answers
  use a numeric maximum restaurant radius from the meetup location, with a
  30+ mile endpoint for dispersed groups; guest home locations are not
  collected in the MVP.
- `RestaurantCandidate`: canonical Google Place plus group-fit score.
- `AvailabilityEvidence`: reservation/waitlist result, source URL, and
  checked timestamp for each candidate/date/time combination.
- `GroupRecommendation`: ranked top three options with score breakdown and
  uncertainty.

## Recommendation pipeline

```text
survey answers
    -> validate and aggregate preferences
    -> select feasible date/time windows
    -> search and hydrate restaurant candidates
    -> check reservation evidence for feasible windows
    -> deterministic scoring
    -> agent explanation and top-three presentation
```

The aggregate and score should remain deterministic and testable. The agent
can choose which candidates to investigate and explain tradeoffs, but it must
not invent guest preferences or turn missing availability evidence into a
positive recommendation.

## Security and privacy

- Use an opaque, revocable response token; do not put guest answers in the URL.
- Rate-limit public response submission and make submissions idempotent.
- Keep organizer authentication and guest participation as separate concerns.
- Store the minimum guest identity needed for the event; anonymous responses
  are the default.
- Never expose the response list through a guest-facing endpoint.

## What is deliberately not shared with HungryRadar yet

The event lifecycle, survey model, guest access model, aggregation rules, and
group ranking are new domain concepts. They should be implemented here first.
If both products later need common code, extract a small neutral package only
after both implementations establish the same contract.
