# Future work

## Production deployment

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
- [ ] Deploy the Strands agent to Amazon Bedrock AgentCore Runtime with a
      least-privilege execution role.
- [ ] Configure AgentCore inbound authorization and pass the verified
      organizer identity as the actor ID. Keep surveys and responses in the
      application database, not AgentCore session state.
- [ ] Connect OpenTable through AgentCore Identity using organizer-delegated
      credentials only when booking is explicitly confirmed.
- [ ] Add CloudWatch logs, metrics, alarms, tracing, and request correlation
      IDs. Redact guest tokens, credentials, and reservation details.

## Application work

- [ ] Add an SMS provider and delivery status tracking.
- [ ] Add organizer authorization checks for every survey-management route.
- [ ] Add survey expiration, revoke, and response export controls.
- [ ] Add deterministic ranking and confidence scoring before agent prose.
- [ ] Persist recommendation runs, hydrated Places candidates, availability
      evidence, and organizer booking decisions.
- [ ] Add retries and idempotency keys around provider calls and booking.
- [ ] Add Postgres migrations and a one-time migration for legacy
      `responses_json` data.

## Local development

- [ ] Add a one-command local runner for API and frontend services.
- [ ] Add a seeded local event command for repeatable demos.
- [ ] Add network-free tests for survey creation, option filtering, duplicate
      response updates, aggregation, and organizer authorization.
- [ ] Add a local Cognito/JWT adapter or documented test-token workflow.
- [ ] Add Playwright smoke coverage for organizer creation and guest voting.
