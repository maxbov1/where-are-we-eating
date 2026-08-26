# HungryRadar Migration Map

This map keeps the new idea separate while identifying useful prior work.

| HungryRadar asset | Decision | How it applies here |
| --- | --- | --- |
| `models.Place` | Reuse concept, then extract | Canonical restaurant record for candidates. Move only into a neutral package after the new app has a consumer. |
| `adapters/google_places.py` | Reuse with a thin adapter boundary | Search and hydrate restaurant candidates; add the fields needed by group ranking. |
| `adapters/google_distance_matrix.py` | Reuse with a thin adapter boundary | Optional travel-fit signal for the group. |
| `adapters/booking_page.py` | Reuse cautiously | Collect reservation-page evidence; keep its uncertainty semantics. |
| `ports.py` | Adapt/extract | Useful provider interfaces, but add candidate search and availability by multiple date/time windows. |
| `decision.py` | Rebuild | Its single-party “reservation wins” ordering does not model votes, conflicts, or group-fit scoring. |
| `lifecycle.py` | Rebuild | Replace the restaurant investigation graph with event creation, survey, response, recommendation, and organizer decision states. |
| `tools/places.py` and `tools/booking.py` | Adapt | Their provider calls may be wrapped by new application services; do not expose HungryRadar session gates directly. |
| `tools/lifecycle.py` | Do not migrate | The in-memory session registry is not appropriate for organizer events or anonymous response links. |
| `agent.py` | Rebuild | The new agent needs event context, aggregate results, bounded candidate selection, and structured output. |
| `config.py` | Adapt | Namespace settings for the new app; do not inherit HungryRadar environment names blindly. |
| HungryRadar tests | Reuse fixtures/patterns | Preserve network-free adapter mocks and evidence-gate tests; add event and aggregation tests here. |
| HungryRadar docs/graph | Do not migrate | It describes a different lifecycle and would confuse implementation agents. |

## Recommended extraction order

1. Implement the new app with local interfaces and small adapter wrappers.
2. Port the `Place` contract and provider mappings only when the first
   restaurant candidate flow is built.
3. Port booking evidence behavior with explicit multi-slot tests.
4. Extract shared restaurant infrastructure into a neutral package only when
   both apps import it without product-specific assumptions.

Do not copy the whole `hungryradar` package into this directory. That would
also copy its single-party assumptions, lifecycle gates, configuration names,
and Strands tool boundary.
