# Hackathon POC Plan

## The demo promise

“I text one link to my group, nobody creates an account or types a paragraph,
and in under two minutes we get three realistic restaurant options the group
can agree on.”

The demo should follow one happy path with six to eight seeded guests. The
organizer creates “Friday dinner,” chooses three dates and three time slots,
shares the link, and opens a live response dashboard. After enough responses,
the organizer clicks **Find our best options** and receives three ranked cards
showing group fit, reservation evidence, and the tradeoff behind each choice.

## Sprint 1 — Coordination loop

Goal: prove the link and response experience.

- Add organizer event creation with a lightweight local/demo sign-in.
- Add editable questions using only buttons, sliders, and selects.
- Support up to three dates and three time slots per date.
- Generate a copyable SMS-friendly response URL.
- Build the guest flow with no account and an idempotent submit action.
- Add seeded/demo data fallback so the presentation never depends on SMS
  delivery or a third-party account.

Exit condition: a guest can answer on mobile in under 30 seconds and the
organizer can see response counts and date/time support.

## Sprint 2 — Group intelligence

Goal: turn scattered answers into an understandable decision.

- Define a small answer vocabulary: cuisine, budget, distance tolerance,
  dietary constraints, and preferred time.
- Implement deterministic aggregation: consensus, split votes, missing
  answers, and participation rate.
- Add a “why this works” summary in plain language.
- Let the organizer adjust the importance of one or two criteria.
- Add a visible confidence indicator when the group is split or response count
  is low.

Exit condition: the dashboard explains both the leading option and the main
disagreement without needing the agent.

## Sprint 3 — Restaurant reality

Goal: connect preferences to places that can plausibly host the group.

- Search Google Places for a bounded set of candidates.
- Hydrate canonical place details using the copied adapter foundation.
- Check the selected date/time windows for reservation-page evidence.
- Rank candidates using group fit plus availability confidence.
- Return exactly three options where possible, with source links and checked
  timestamps; label uncertainty explicitly.

Exit condition: the organizer sees three actionable restaurant cards, not a
generic list of highly rated places.

## Sprint 4 — Agent polish and pitch

Goal: make the result feel magical and safe.

- Use the agent to select which candidates need deeper investigation and to
  write the concise explanation.
- Keep aggregation, scoring, and evidence gates deterministic around it.
- Add organizer actions: choose option, copy booking link, reopen responses.
- Add a resettable seeded demo event and a “what happens if preferences
  conflict?” state.
- Add one slide/section explaining privacy: guests respond anonymously and
  only the organizer sees the group summary.

Exit condition: a complete demo runs in three minutes, including one edge case.

## Cut list if time gets tight

- Real SMS sending: use copy/share URL plus a seeded guest simulator.
- Automatic booking: deep-link to the restaurant booking path.
- Arbitrary survey builder: ship five fixed question templates with editable
  wording.
- Live multi-user sync: use refresh/polling or a local demo state first.
- Broad restaurant search: use one city and a small candidate set.

## Success measures

- Guest completion rate in the demo: at least 80% of seeded guests.
- Guest response time: under 30 seconds.
- Organizer time from event creation to top three: under two minutes.
- Every recommendation card has a visible rationale, source, and uncertainty
  state.
