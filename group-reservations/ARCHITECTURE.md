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

## Domain objects

- `Event`: organizer, title, status, response URL/token, survey, candidate
  dates, and candidate time slots.
- `SurveyQuestion`: stable key, prompt, answer type, options, and active state.
- `GuestResponse`: event, opaque respondent token, structured answers,
  submitted timestamp, and revision timestamp.
- `PreferenceSummary`: response count, participation rate, per-option support,
  conflicts, and confidence.
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
