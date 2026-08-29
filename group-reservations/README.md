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

# Copy the safe defaults.
cp .env.example .env

# Put your Google Places key in .env as GOOGLE_MAPS_API_KEY.
# Configure AWS with `aws login` or an IAM profile; see below.

PYTHONPATH=src python -m groupreservations.opentable_mcp --check-config

PYTHONPATH=src python -m groupreservations.opentable_mcp --list-tools
PYTHONPATH=src python -m groupreservations.opentable_mcp \
  "Find three Italian restaurants for 4 people in San Francisco this Friday at 7 PM"

# Start the API used by the frontend in a second terminal.
PYTHONPATH=src uvicorn groupreservations.api:app --reload --port 8000

# Serve the frontend in a third terminal.
python -m http.server 4173 --directory frontend
```

The agent exposes `google_places_search` and `google_places_details` first.
Search returns hydrated canonical restaurant structs from Google Places before
any reservation checks. It then allowlists only the OpenTable tools needed for
availability and booking:
`opentable_status`, `opentable_login`, `opentable_check_availability`, and
`opentable_make_reservation`. OpenTable search and restaurant-detail tools are
not used because Google Places owns discovery and place identity. Cancellation
and reservation-history tools remain excluded.

### Organizer booking handoff

OpenTable login is deferred until the organizer explicitly confirms a booking.
Restaurant discovery and availability checks do not require an OpenTable login.
Booking requires the exact restaurant, date, time, and party size to be
confirmed by the organizer. No OpenTable password passes through the survey,
prompt, or agent logs.

The app JWT is only our identity token. Its `sub` claim is the organizer ID.
Every request must verify that token before creating the OpenTable MCP client,
then pass that organizer ID to `create_opentable_client(user_id)`. That client
launches with `HOME=.local/opentable-sessions/<user_id>/home`, so the MCP
server's cookie file at `~/.strider/opentable/cookies.json` is isolated per
organizer. Never use one long-lived global MCP process for multiple users.

For the local POC, mint tokens with `GROUP_RESERVATIONS_JWT_SECRET` and use
`mint_access_token`; in the web app, the existing organizer auth service should
own token issuance instead. JWTs do not authenticate to OpenTable and must not
be sent to the OpenTable MCP server.

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

# Install Playwright browsers if OpenTable reports they are missing.
npx playwright install
```

Strands uses Amazon Bedrock through an explicitly configured `BedrockModel`.
The default model is `global.anthropic.claude-sonnet-4-6`; change it with
`GROUP_RESERVATIONS_MODEL_ID`.

The frontend sends `POST /api/recommendations` with the event’s dates, time
slots, and structured guest responses. The API converts that payload into an
agent prompt and returns `{ "status": "ok", "answer": "..." }`. This stable
HTTP boundary is intended to be wrapped by AgentCore later; the frontend does
not need to know how the agent is hosted.

The local API also provides `POST /api/users` for organizer records,
`POST /api/surveys` to create a survey, `GET /api/surveys/{public_token}` for
the public survey shell, `POST /api/surveys/{public_token}/responses` for
guest answers, and `GET /api/surveys/{survey_id}/aggregate` for cleaned agent
context. Guests receive a temporary user row with no email or account
credentials. Their token is hashed, and each response is stored independently.
The API mints links from `PUBLIC_APP_URL`; set that value to the deployed web
app origin when hosting outside localhost.

## Repository Layout

```text
src/groupreservations/
├── adapters/google_places.py  # Google Places REST adapter
├── auth.py                    # Organizer JWT helpers
├── config.py                  # Environment-backed settings
├── models.py                  # Place and availability structs
├── opentable_mcp.py           # Strands agent/OpenTable boundary
├── places_tools.py            # Hydrated Google Places tools
└── ports.py                   # Provider protocols
```

## Documentation Map

- [ARCHITECTURE.md](ARCHITECTURE.md): components, data flow, and security.
- [CONTRIBUTING.md](CONTRIBUTING.md): developer setup and checks.
- [POC_PLAN.md](POC_PLAN.md): sprint plan and demo target.
- [MIGRATION.md](MIGRATION.md): copied HungryRadar foundation and boundaries.
- [AGENTS.md](AGENTS.md): repository constraints for coding agents.
