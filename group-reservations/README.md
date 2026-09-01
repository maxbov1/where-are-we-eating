# Where Are We Eating?

An organizer creates a lightweight coordination link, sends it by SMS, and
lets each guest express preferences without typing. The agent combines the
responses and recommends up to three restaurants, dates, and times that best
fit the group.

This is intentionally a separate product from HungryRadar. HungryRadar asks
“can I eat there now?”; Group Reservations asks “what can this group agree on,
and where can they actually reserve?”

## What This Repository Does

The current local POC demonstrates the restaurant evidence portion of the
product:

- Google Places discovers restaurants and hydrates canonical `Place` structs
  with addresses, ratings, links, hours, reservation support, and checked
  timestamps.
- Strands orchestrates Google Places and the OpenTable MCP.
- OpenTable is limited to availability checks and explicit booking actions.
- Exact Google Reserve and restaurant booking pages are inspected first when
  available.
- OpenTable login is deferred until the organizer confirms a booking.
- Organizers can configure guest-facing question options, currently including
  cuisine choices, without changing the survey code.
- Organizer JWTs and isolated child-process `HOME` directories prevent
  third-party session cookies from being shared between organizers.

The local frontend now sends its structured survey payload to the agent API.
SQLite mirrors the production model: Cognito-sub keyed organizers, hashed
anonymous guest tokens, normalized questions/options, independent responses,
and deterministic aggregation. Production deployment adds Cognito, Aurora
PostgreSQL, and SMS delivery. See [POC_PLAN.md](POC_PLAN.md).

## MVP flow

1. The signed-in organizer creates an event.
2. They customize a short, button/slider/select-only survey.
3. They choose up to three candidate dates and three time slots per date.
4. The app generates a public response URL suitable for SMS.
5. Guests respond without creating accounts.
6. The agent aggregates preferences, searches candidate restaurants, and
   returns a ranked top three with the reasoning and reservation evidence.
7. The organizer chooses an option and continues to the restaurant’s booking
   path.

## Product boundaries

- Only organizers need accounts in the MVP.
- Guest responses are scoped to an event link and should be revocable by the
  organizer.
- Survey answers are structured data; free-form typing is out of scope for
  the first slice.
- The agent recommends and explains. Booking requires explicit organizer
  confirmation; cancellation and reservation modification are not exposed.
- Restaurant facts and reservation evidence must retain their source URL and
  checked timestamp.

## Suggested first implementation slices

- Event creation, organizer authentication, and shareable response links.
- Survey schema with editable questions and date/time options.
- Anonymous guest response submission with duplicate-response handling.
- Deterministic preference aggregation before adding agent orchestration.
- Restaurant candidate hydration and availability evidence.
- Ranked recommendation cards with an organizer decision state.

## Next three things

1. Troubleshoot OpenTable MCP reliability: capture the failing request, fix
   the upstream cleanup race, and determine whether OpenTable blocks the
   browser session before treating live availability as production-ready.
2. Replace free-form agent output with a versioned recommendation schema that
   carries restaurant evidence, booking provider URLs, availability status,
   and confidence without duplicating prose.
3. Build the production seam: Cognito organizer identity, Aurora PostgreSQL,
   Secrets Manager, AgentCore deployment, and an async job/status path for
   recommendation runs.

See [MIGRATION.md](MIGRATION.md) for what should move from HungryRadar and
[ARCHITECTURE.md](ARCHITECTURE.md) for the proposed seams.

The first copied foundation lives under `src/groupreservations/`. It is an
independent copy: HungryRadar remains unchanged and the new package does not
import from it.

See [POC_PLAN.md](POC_PLAN.md) for the sprint plan and demo target.

## Quick Start

The POC uses Google Places for restaurant discovery and canonical restaurant
details. The community OpenTable MCP server is used only for availability checks
and the explicit organizer-confirmed booking flow.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Install the Python Playwright runtime used by the adaptive booking browser.
python -m playwright install chromium

# Copy the safe defaults.
cp .env.example .env

# Put your Google Places key in .env as GOOGLE_MAPS_API_KEY.
# Configure AWS with `aws login` or an IAM profile; see below.

PYTHONPATH=src python -m groupreservations.opentable_mcp --check-config

PYTHONPATH=src python -m groupreservations.opentable_mcp --list-tools
PYTHONPATH=src python -m groupreservations.opentable_mcp \
  "Find three Italian restaurants for 4 people in San Francisco this Friday at 7 PM"

# Start the API (http://127.0.0.1:8000) and the frontend
# (http://127.0.0.1:4173) together. Ctrl+C stops both.
python scripts/dev.py

# Override ports or disable autoreload if needed.
python scripts/dev.py --api-port 8001 --frontend-port 5173 --no-reload

# Or run them by hand in two terminals instead:
#   PYTHONPATH=src uvicorn groupreservations.api:app --reload --port 8000
#   python -m http.server 4173 --directory frontend

# Seed five repeatable guest responses for the recommendation flow.
PYTHONPATH=src python scripts/seed_fixture.py

# Run fixture → cleaned report → Google Places/Bedrock agent end to end.
PYTHONPATH=src python scripts/run_fixture_flow.py
```

The agent exposes `google_places_search` and `google_places_details` first.
Search returns hydrated canonical restaurant structs from Google Places before
any reservation checks. It then allowlists only the OpenTable tools needed for
availability and booking:
`opentable_status`, `opentable_login`, `opentable_search`,
`opentable_get_restaurant`, `opentable_check_availability`, and
`opentable_make_reservation`. Google Places remains the primary discovery
source. When a restaurant exposes an exact Google Reserve or provider booking
path, the agent uses that path first; otherwise OpenTable search must resolve a
real record before availability is checked. Cancellation and
reservation-history tools remain excluded.

### Run the canned recommendation flow

`tests/fixtures/san-clemente-dinner.json` contains one event and five guest
response patterns with limited date/time crossover and dietary constraints.
Seed it, copy the printed `survey_id`, and inspect the
cleaned vote summary:

```bash
PYTHONPATH=src python scripts/seed_fixture.py
curl http://127.0.0.1:8000/api/surveys/<survey_id>/aggregate
```

The aggregate response and its `report` carry a deterministic `confidence`
block: a per-dimension score, an overall `high`/`medium`/`low`/`none` band, the
weakest dimension, and plain-language notes (low response count, split votes,
unanswered questions). It is computed in `scoring.py` from the vote tallies
alone, before any agent call, and the agent is instructed to honor it.

To run the real Bedrock-backed recommendation path against those responses:

```bash
curl -X POST http://127.0.0.1:8000/api/surveys/<survey_id>/recommendations \
  -H 'X-Organizer-Id: <organizer_id>'
```

This uses the canned preferences, Google Places discovery, and the configured
Bedrock model. OpenTable remains limited to availability and explicit booking.

The one-command version creates a fresh fixture survey, prints the authoritative
cleaned report, then sends that report to the agent:

```bash
PYTHONPATH=src python scripts/run_fixture_flow.py
```

Use `--aggregate-only` to validate cleaning without calling AWS or provider
tools.

### Organizer booking handoff

OpenTable login is deferred until the organizer explicitly confirms a booking.
Restaurant discovery and availability checks do not require an OpenTable login.
Booking requires the exact restaurant, date, time, and party size to be
confirmed by the organizer. No OpenTable password passes through the survey,
prompt, or agent logs.

The app JWT is only our identity token. Its `sub` claim is the organizer ID.
Production requests must verify that token before creating the OpenTable MCP client,
then pass that organizer ID to `create_opentable_client(user_id)`. That client
launches with `HOME=.local/opentable-sessions/<user_id>/home`, so the MCP
server's cookie file at `~/.strider/opentable/cookies.json` is isolated per
organizer. Never use one long-lived global MCP process for multiple users.

For the local POC, mint tokens with `GROUP_RESERVATIONS_JWT_SECRET` and use
`mint_access_token`; in the web app, the existing organizer auth service should
own token issuance instead. JWTs do not authenticate to OpenTable and must not
be sent to the OpenTable MCP server.

The recommendation endpoints accept `Authorization: Bearer <token>`. To make
a local development token:

```bash
export GROUP_RESERVATIONS_JWT_SECRET='use-a-local-secret-with-at-least-32-characters'
PYTHONPATH=src python -c 'from groupreservations.auth import mint_access_token; print(mint_access_token("local-organizer"))'
```

The legacy `X-Organizer-Id` header remains available for local-only calls while
the Cognito/API Gateway boundary is being built.

### Booking discovery and handoffs

For each candidate, the agent opens the exact restaurant website and inspects
anchors, forms, iframes, buttons, and booking data attributes. It prefers an
exact Google Reserve URL, then embedded OpenTable/Resy/Tock links, and finally
the exact restaurant page. OpenTable `restref` values found in website markup
are used to generate a prefilled availability URL with the selected survey
date, time, and party size. The agent never derives a provider ID from a
restaurant name.

## Google Places Setup

Create a project and API key in the
[Google Cloud Console](https://console.cloud.google.com/). Enable **Places API
(New)**, configure billing as required by Google Maps Platform, create an API
key under APIs & Services → Credentials, and restrict the key to Places API
(New). Put it only in your local `.env`:

```env
GOOGLE_MAPS_API_KEY=your-google-places-api-key
```

Never commit `.env` or share the key in prompts, issues, or chat.

## AWS Bedrock Setup

Use the AWS account and region where you will run the POC. The default region is
`us-west-2`. In the [Amazon Bedrock Console](https://console.aws.amazon.com/bedrock/),
enable a model available to the account. Anthropic models may require the
Anthropic first-time-use form and AWS Marketplace/payment approval. For the
fastest demo, set `GROUP_RESERVATIONS_MODEL_ID=us.amazon.nova-lite-v1:0` if that
model is enabled for the account.

Prefer an IAM user, IAM Identity Center profile, or federated role with least
privilege. Do not use AWS root access keys. For browser-based local credentials:

```bash
aws login --region us-west-2
aws sts get-caller-identity
aws configure list
```

For a named profile, see
[AWS CLI configuration](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-files.html):

```bash
aws configure --profile where-are-we-eating
export AWS_PROFILE=where-are-we-eating
```

See [AWS Bedrock model access](https://docs.aws.amazon.com/bedrock/latest/userguide/model-access.html)
and [AWS CLI sign-in](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-sign-in.html)
for account access and credential details.

## Common Commands

```bash
# Show provider configuration without printing credentials.
PYTHONPATH=src python -m groupreservations.opentable_mcp --check-config

# List the OpenTable tools exposed by the agent.
PYTHONPATH=src python -m groupreservations.opentable_mcp --list-tools

# Search and hydrate Google Places candidates, then check availability.
PYTHONPATH=src python -m groupreservations.opentable_mcp \
  "Find three Italian restaurants for 4 people in San Francisco this Friday at 7 PM"

# Install the Chromium binary used by the OpenTable MCP.
npx playwright install chromium
```

Strands uses Amazon Bedrock through an explicitly configured `BedrockModel`.
The default model is `global.anthropic.claude-sonnet-4-6`; change it with
`GROUP_RESERVATIONS_MODEL_ID`.

The frontend sends `POST /api/recommendations` with the event’s dates, time
slots, and structured guest responses. The API converts that payload into an
agent prompt and returns `{ "status": "ok", "answer": "..." }`. This stable
HTTP boundary is intended to be wrapped by AgentCore later; the frontend does
not need to know how the agent is hosted.

Recommendation output keeps booking links separate from provider evidence:
Google Maps, the restaurant website, and an explicit `booking_uri` are carried
on the hydrated `Place` record. The booking URI is resolved from the
restaurant's own website when an explicit reservation link is present; it can
also come from OpenTable, Resy, Tock, or another provider returned by the
agent. The frontend turns exact URLs into clickable links. A booking link is a
handoff, not proof that the selected date/time is available, and the agent
must never guess one from a restaurant name.

When a verified OpenTable ID or exact provider URL is available, the booking
handoff uses OpenTable's current availability shape:

```text
https://www.opentable.com/booking/restref/availability?restref=<verified-id>&dateTime=<ISO-local-time>&covers=<party-size>
```

Existing provider parameters such as `lang`, `ot_source`, and `corrid` are
preserved. The URL format may be inferred, but the `restref` value never is.
If OpenTable cannot return a verified record, availability stays unknown and
the exact restaurant booking page is retained for the organizer.

The local API also provides `POST /api/users` for organizer records,
`POST /api/surveys` to create a survey, `GET /api/surveys/{public_token}` for
the public survey shell, `POST /api/surveys/{public_token}/responses` for
guest answers, and `GET /api/surveys/{survey_id}/aggregate` for cleaned agent
context. Guests receive a temporary user row with no email or account
credentials. Their token is hashed, and each response is stored independently.
`/api/locations/autocomplete` and `/api/locations/details` proxy Google Places
Autocomplete (New) and Place Details (New). Organizer locations are city-only;
guests may optionally select a neighborhood, landmark, ZIP code, or nearby
place. The guest origin stores a selected Place ID and coordinates, not a raw
home address, and is intended for private group travel-fairness ranking.
The default guest survey uses explicit per-person budget bands and a stepped
restaurant search radius from 1 to 30+ miles around the meetup location. The
radius is a search boundary, not a claim about where guests live; the 30+
endpoint keeps the question useful for groups spread across a metro area.
The organizer location field has browser-native validation and suggestions;
authoritative Google Places location normalization remains a future provider
seam.
The API mints links from `PUBLIC_APP_URL`; set that value to the deployed web
app origin when hosting outside localhost.

## Repository Layout

```text
src/groupreservations/
├── adapters/google_places.py  # Google Places REST adapter
├── auth.py                    # Organizer JWT helpers
├── config.py                  # Environment-backed settings
├── models.py                  # Place and availability structs
├── booking.py                 # Provider-aware booking URL handoffs
├── opentable_mcp.py           # Strands agent/OpenTable boundary
├── places_tools.py            # Hydrated Google Places tools
├── reservation_browser.py     # Serialized adaptive booking browser
└── ports.py                   # Provider protocols
```

## Documentation Map

- [ARCHITECTURE.md](ARCHITECTURE.md): components, data flow, and security.
- [CONTRIBUTING.md](CONTRIBUTING.md): developer setup and checks.
- [POC_PLAN.md](POC_PLAN.md): sprint plan and demo target.
- [MIGRATION.md](MIGRATION.md): copied HungryRadar foundation and boundaries.
- [AGENTS.md](AGENTS.md): repository constraints for coding agents.
