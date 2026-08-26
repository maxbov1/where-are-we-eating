# Group Reservations

An organizer creates a lightweight coordination link, sends it by SMS, and
lets each guest express preferences without typing. The agent combines the
responses and recommends up to three restaurants, dates, and times that best
fit the group.

This is intentionally a separate product from HungryRadar. HungryRadar asks
“can I eat there now?”; Group Reservations asks “what can this group agree on,
and where can they actually reserve?”

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
- The agent recommends and explains. It does not automatically book, cancel,
  or modify a reservation.
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
