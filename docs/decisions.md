# Decisions and open questions

## Decided

| # | Decision | Rationale |
|---|---|---|
| 1 | **Python 3.11** | The deterministic layer is numeric/optimization work. Also the shortest path to MCP and a later FastAPI surface. |
| 2 | **SQLite now, Postgres-portable** | Zero setup; a versioned `.db` file *is* a reproducible snapshot. Alembic runs with `render_as_batch` so the same scripts apply to Postgres. |
| 3 | **ESPN first platform** | Owner's league is on ESPN. Read-only public JSON; private leagues use cookie auth. |
| 4 | **CLI + MCP first interface** | Thin handlers over the engine, so chat/web/API layer on later. |
| 5 | **nflverse for identity and context** | Free, CC-BY-4.0, actively maintained, and carries the cross-platform ID crosswalk that removes the biggest silent-failure risk. |
| 6 | **Projections stored as canonical stat lines, not points** | One ingested projection can be priced under any league's rules, and re-priced correctly if settings change mid-season. |
| 7 | **Exact lineup optimization, not greedy** | Slot eligibility forms a transversal matroid, so weight-ordered greedy with augmenting paths is provably optimal. Greedy-by-slot-restrictiveness is wrong in the common FLEX-displacement case. |
| 8 | **Roster membership as intervals** | Closing an interval instead of deleting a row lets any past week be reconstructed exactly. |
| 9 | **Scoring changes create a new `league_rules` version** | Never mutate history, or old recommendations become uninterpretable. |
| 10 | **Ambiguous names raise rather than guess** | Drafting the wrong Mike Williams is worse than asking again. |

## Open — needed from the owner

Not blocking scaffolding, but blocking real use:

1. **ESPN league ID** (`DRAFTGPT_ESPN_LEAGUE_ID`), and `espn_s2`/`SWID` cookies
   if the league is private. Everything against your real league is blocked on
   this; the fixture path works without it.
2. **Which team is yours** — set via `draftgpt league set-owner <team-id>`.
3. **Projection source.** The single biggest quality lever still unresolved. See
   `docs/sources.md`; the default fallback is ESPN's own projections.
4. **Risk preference** — floor, balanced, or ceiling. Currently defaults to
   balanced per command invocation; should become a stored user preference.
5. **Keeper rules**, if any — keeper costs and traded picks are modelled in the
   schema but not yet populated.
6. **Licence for this repository.** Not yet chosen. Note that if
   `load_ff_rankings` is ever promoted from optional to load-bearing, the
   GPL-3.0 upstream needs a considered answer.

## Assumptions made

Documented rather than asked, per the brief. Each is cheap to reverse.

- **Season defaults to 2026** (`DRAFTGPT_SEASON`).
- **Flex share weights** for replacement-level calculation are RB 0.42 /
  WR 0.48 / TE 0.10, and QB 0.85 in superflex. These are estimates and are
  configurable in `evaluation/replacement.py`.
- **Floor/ceiling fallbacks** when a projection supplies neither: floor = 0.6 ×
  median, ceiling = 1.5 × median. Crude, and flagged as such in the code.
- **Objective weights**: floor mode = 0.55 floor + 0.45 median; ceiling mode =
  0.45 median + 0.55 ceiling. Tunable in `evaluation/lineup.py`.
- **Bye weeks are derived from the schedule**, not hardcoded — a team's bye is
  the regular-season week it does not appear in.
- **Players on bye or designated OUT are excluded from optimization**, not
  scored at zero, so they can never displace a startable player through a tie.
- **A missing projection is treated as 0.0 and reported**, never silently
  imputed. Confidence drops accordingly.
- **ESPN's `temp`/`wind` being null means unknown**, not good weather. Callers
  must not read absence as a positive signal.
- **Confidence reflects decision margin**, not model certainty: a lineup whose
  last starter beats the first bench player by 0.3 points should say it is close
  regardless of how fresh the data is.

## Known gaps in Phase 1

Stated plainly rather than buried:

- `/draft`, `/pulse`, `/waiver`, `/trade` are **not implemented**. The schema
  supports them; the handlers do not exist.
- **ESPN stat-id map is unverified** against a real league. `draftgpt sources
  verify-espn` exists to close this and must be run before trusting projections.
- **No projection source is selected**, so the only projections available today
  are fixtures or manual CSV import.
- **The explanation layer is scaffolded but not implemented.** The contract
  restricts it to `material_inputs`; nothing calls an LLM yet.
- **No scheduler.** Refresh cadence is defined in the freshness policy but
  nothing runs it automatically; all ingestion is manual today.
- **`marginal_value` re-optimizes per call**, which is O(n) full solves for a
  bench list. Fine at roster scale, needs memoization before `/trade`.
