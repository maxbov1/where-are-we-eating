# Agent Instructions

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
