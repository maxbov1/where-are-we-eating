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

- [ ] Add network-free tests for survey creation, option filtering, duplicate
      response updates, aggregation, and organizer authorization.
- [ ] Add deterministic restaurant ranking before agent prose. Blocked until
      restaurant candidates are hydrated outside the agent; today the agent
      still does discovery and ranking in one call. Group-preference confidence
      scoring is done (see above).
- [x] Add a local Cognito/JWT adapter or documented test-token workflow.
      `auth.py` already has `mint_access_token` / `verify_access_token`; wire
      them into the API and document a dev token.

## Tier 2 — Application features

- [ ] Add organizer authorization checks for every survey-management route.
      Routes currently trust a browser-supplied `X-Organizer-Id` / body field.
- [ ] Add survey expiration, revoke, and response export controls.
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
- [ ] Add production provider integrations through AgentCore Identity using
      organizer-delegated credentials only when booking is explicitly
      confirmed. The local agent currently uses verified browser pages and
      does not expose provider mutation tools.
