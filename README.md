# draftgpt

A league-specific fantasy football decision engine. **Read-only**: it tells you
what to do, it never does it for you. No lineups are submitted, no players
added, no trades proposed through any platform.

The database is authoritative. Structured state, calculations, and source
timestamps are the source of truth for availability, scoring, projections,
eligibility, rosters, and transactions. An LLM may synthesize and explain — it
may never introduce a fact.

## Status

**Phase 1 — canonical state and vertical slice.** Schema, migrations, provider
adapters, freshness tracking, and `/roster` end to end. `/draft`, `/pulse`,
`/waiver`, and `/trade` are scaffolded but not yet implemented.

**Interface: terminal CLI only.** There is no web UI and no MCP server yet.

## Read-only guarantee

This system never writes to a fantasy platform. That is enforced structurally,
not by policy:

- The ESPN client exposes a single `get()` method. No `post`, `put`, `patch`, or
  `delete` call exists anywhere in the codebase.
- No adapter declares a write capability; the provider contract has no verb for
  one.
- Nothing submits a lineup, adds or drops a player, places a waiver claim, or
  proposes a trade. Those actions remain entirely yours to take in your league.

Reading a **private** ESPN league requires `espn_s2` and `SWID` cookies from a
logged-in browser session. Those are your ESPN session credentials: they live in
`.env` (gitignored), are never logged, and are used only for read requests. If
you would rather not supply them, the system still runs on fixtures.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env          # set DRAFTGPT_ESPN_LEAGUE_ID at minimum
alembic upgrade head          # create the schema

draftgpt sources register     # register provider adapters
draftgpt sync players         # canonical players + cross-platform ID crosswalk
draftgpt sync league          # league settings, rules, teams, rosters
draftgpt roster week 1        # the vertical slice
```

To try it with no credentials at all, use the bundled fixture league:

```bash
draftgpt demo                 # seeds a fixture league and runs /roster
```

## Commands

| Command | Status | Purpose |
|---|---|---|
| `prep league` | **implemented** | What your settings imply for strategy, with evidence |
| `prep draft` | **implemented** | Draft board by value over replacement, tiers, cliffs, scarcity |
| `prep player` | **implemented** | One player's value, tier, ADP, and availability |
| `/roster` | **implemented** | Lineup recommendation, conditional swaps, next check time |
| `/draft` | Phase 2 | Live draft tracking and pick recommendations |
| `/pulse` | Phase 3 | Material changes since last acknowledgement |
| `/waiver` | Phase 3 | Ordered claim plan with FAAB bids |
| `/trade` | Phase 4 | Two-sided lineup-delta trade evaluation |

Every command returns the same structured contract: recommendation,
alternatives, confidence, reasons, material inputs, freshness, conditional
triggers, and when to check again.

## Talking to it from a chat client (MCP)

The prep tools are exposed over MCP so you can ask questions in conversation
instead of running commands:

```bash
pip install -e ".[mcp]"
```

Then register the server with your client. For Claude Code:

```bash
claude mcp add draftgpt -- /absolute/path/to/.venv/bin/draftgpt mcp
```

Or add it to a client config directly:

```json
{
  "mcpServers": {
    "draftgpt": {
      "command": "/absolute/path/to/.venv/bin/draftgpt",
      "args": ["mcp"],
      "env": { "DRAFTGPT_DATABASE_URL": "sqlite:////absolute/path/to/data/draftgpt.db" }
    }
  }
}
```

Tools exposed: `league_status`, `prep_league`, `prep_draft_board`,
`prep_player`, `roster_recommend`. Every one is declared `readOnlyHint: true`
at the protocol level.

The server returns computed evidence rather than prose, and its instructions
forbid asserting anything absent from the tool output — so the assistant
narrates the database rather than its own recollection of fantasy football.

## Architecture

```
providers/     source adapters + capability contracts (espn, nflverse, fixture)
ingestion/     run lifecycle, freshness tracking, identity reconciliation, sync
database/      schema, migrations, models
domain/        vocabularies, freshness policy, response contract
evaluation/    deterministic scoring, lineup optimization, replacement value
commands/      thin handlers over the engine
interfaces/    CLI (an MCP server is planned but NOT yet built)
```

Three layers, kept separate on purpose:

1. **State assembly** builds a validated decision snapshot, bounded by `as_of`
   so replays never see future information.
2. **Deterministic evaluation** does all the maths. The optimal lineup is an
   exact maximum-weight assignment (slot eligibility forms a transversal
   matroid), not a greedy approximation.
3. **Explanation** is optional and may only reference `material_inputs`.

## Data sources

| Category | Provider | Licence | Status |
|---|---|---|---|
| League state | ESPN (read-only JSON) | see below | implemented |
| Player identity crosswalk | nflverse `load_rosters` | CC-BY-4.0 | implemented |
| Schedule, weather, betting lines | nflverse `load_schedules` | CC-BY-4.0 | implemented |
| Injuries + practice participation | nflverse `load_injuries` | CC-BY-4.0 | implemented |
| Depth charts | nflverse `load_depth_charts` | CC-BY-4.0 | implemented |
| Usage / snap share | nflverse `load_snap_counts` | CC-BY-4.0 | implemented |
| Weekly projections | **not selected** | — | fixture / CSV import |
| ADP | **not selected** | — | fixture / CSV import |

See [`docs/sources.md`](docs/sources.md) for the full adapter register,
including licensing posture and fallback behaviour for each.

### Attribution

This project uses data from **[nflverse](https://nflverse.com)**, licensed under
[CC-BY-4.0](https://creativecommons.org/licenses/by/4.0/). Accessed via
[`nflreadpy`](https://github.com/nflverse/nflreadpy) (MIT).

FTN charting data (CC-BY-SA-4.0) is deliberately **not** used, so no share-alike
obligation attaches to this project.

ESPN endpoints are the publicly readable JSON APIs used by ESPN's own web
client, accessed read-only at low volume for a league the user belongs to. No
HTML scraping and no redistribution of payloads.

## Reproducibility

Every recommendation persists a `decision_snapshot` (hashed inputs plus a
serialized payload) and a `recommendation_run` stamped with the calculation
version. Past advice can be replayed exactly, and backtested without leaking
future information.

## Development

```bash
pytest              # unit, integration, and golden-scenario tests
ruff check .
mypy
```

## Licence

Not yet selected — see `docs/decisions.md`.
