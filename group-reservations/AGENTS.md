# Agent Instructions

## Repository Overview

This is the Where Are We Eating? group-reservation POC. Google Places is the
primary restaurant discovery and hydration provider. OpenTable MCP is limited
to availability and explicit booking. The project is intentionally separate
from `hungryradar`.

## High-Risk Areas

- Keep organizer authentication, guest response access, and event ownership
  explicit in the domain model.
- Treat survey answers as structured, validated data; do not require typing in
  the MVP.
- Keep aggregation and ranking deterministic and unit-testable without network
  access.
- Preserve source URLs and checked timestamps for restaurant and reservation
  evidence.
- Missing or stale evidence must remain uncertainty, never a positive booking
  claim.
- Do not import from `hungryradar` directly until a shared package boundary is
  intentionally designed and documented.

## Required Constraints

- Never commit `.env`, API keys, AWS credentials, JWT secrets, passwords, or
  browser cookies.
- Never use AWS root access keys in development instructions.
- Do not expose OpenTable search or restaurant identity as a competing source;
  Google Places owns candidate discovery.
- Do not expose reservation mutation tools without exact organizer confirmation
  of restaurant, date, time, and party size.
- Preserve provider source URLs and checked timestamps on returned evidence.

## Preferred Workflows and Tooling

- Use the Google Places adapter boundary for provider calls and mock it in tests.
- Run `python -m compileall -q src`, `PYTHONPATH=src python -m pytest`, and
  `git diff --check` before handoff.
- Update README and ARCHITECTURE.md when capabilities, setup, provider scope,
  or security boundaries change.
