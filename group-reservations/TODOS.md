# Future work

Remaining work is grouped into three tiers by difficulty:

- **Tier 1** — Local, self-contained, no infrastructure. Pure Python or test
  code that runs on a laptop.
- **Tier 2** — Application features inside this codebase. Touch several routes,
  the schema, or the frontend; some need an external vendor account.
- **Tier 3** — Requires standing up the production AWS stack (Cognito, Aurora,
  API Gateway, AgentCore, CloudWatch).

## Done

- [x] Add a one-command local runner for API and frontend services.
      `scripts/dev.py` (or `npm run dev`) starts both and stops both on Ctrl+C.
- [x] Add a seeded local event command for repeatable demos.
      `scripts/seed_fixture.py` seeds the San Clemente fixture and prints the
      `survey_id`; `scripts/run_fixture_flow.py` runs it end to end.
- [x] Add deterministic confidence scoring before agent prose.
      `scoring.py` turns the aggregate's vote tallies and consensus labels into
      a `confidence` block (per-dimension score, overall band, weakest
      dimension, plain-language notes). `aggregate_survey` attaches it to the
      report and `_agent_prompt` instructs the agent to honor it.

## Tier 1 — Local, no infrastructure

- [x] Add network-free tests for survey creation, option filtering, duplicate
      response updates, aggregation, and organizer authorization. Creation,
      filtering, duplicate updates, and aggregation live in
      `tests/test_database.py`; the organizer-authorization boundary is covered
      end to end in `tests/test_api_authorization.py`.
- [ ] Add deterministic restaurant ranking before agent prose. Blocked until
      restaurant candidates are hydrated outside the agent; today the agent
      still does discovery and ranking in one call. Group-preference confidence
      scoring is done (see above).
- [x] Add a local Cognito/JWT adapter or documented test-token workflow.
      `auth.py` already has `mint_access_token` / `verify_access_token`; wire
      them into the API and document a dev token.

## Tier 2 — Application features

- [x] Add organizer authorization checks for every survey-management route.
      `api.py` now resolves the organizer from a verified bearer token or the
      legacy `X-Organizer-Id` header (never the request body), rejects
      unauthenticated calls with `401`, and returns `403` when the caller does
      not own the referenced survey. Guest routes keyed by `public_token` stay
      public. Covered by `tests/test_api_authorization.py`.
- [x] Add survey expiration, revoke, and response export controls.
      `surveys.expires_at` / `surveys.revoked_at` drive a derived
      `active`/`expired`/`revoked` status. A new survey expires a grace day past
      its last candidate date so no link stays open forever. `POST
      /api/surveys/{id}/revoke`, `POST /api/surveys/{id}/expiration`, and `GET
      /api/surveys/{id}/responses/export?format=csv|json` are organizer-only.
      Closing returns `410 Gone` to guests while leaving the organizer's
      aggregate, export, and recommendation routes open. Covered by
      `tests/test_survey_lifecycle.py` and `tests/test_api_survey_controls.py`.
- [ ] Add Playwright smoke coverage for organizer creation and guest voting.
- [ ] Add retries and idempotency keys around provider calls and booking.
- [ ] Persist recommendation runs, hydrated Places candidates, availability
      evidence, and organizer booking decisions.
- [ ] Add an SMS provider and delivery status tracking. Needs a vendor account
      (Twilio, SNS, etc.) but not the full AWS production stack.

## Tier 3 — Requires the production AWS stack

- [ ] Add Postgres migrations and a one-time migration for legacy
      `responses_json` data.
- [ ] Create an Amazon Cognito User Pool and configure API Gateway JWT
      authorization. The API must derive the organizer identity from the
      verified Cognito `sub`, not from a browser-supplied ID.
- [ ] Replace the local SQLite adapter with Aurora PostgreSQL Serverless v2.
- [ ] Put RDS Proxy in front of Aurora for Lambda/ECS connection pooling.
- [ ] Store database credentials and third-party secrets in AWS Secrets
      Manager, encrypted with KMS. Never put them in AgentCore environment
      variables or prompts.
- [ ] Deploy the FastAPI service behind API Gateway and the frontend through
      S3 + CloudFront.
- [ ] Add CloudWatch logs, metrics, alarms, tracing, and request correlation
      IDs. Redact guest tokens, credentials, and reservation details.
- [ ] Deploy the Strands agent to Amazon Bedrock AgentCore Runtime with a
      least-privilege execution role.
- [ ] Configure AgentCore inbound authorization and pass the verified
      organizer identity as the actor ID. Keep surveys and responses in the
      application database, not AgentCore session state.
- [ ] Connect OpenTable through AgentCore Identity using organizer-delegated
      credentials only when booking is explicitly confirmed.
