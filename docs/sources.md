# Source register

Every external input enters through an adapter that declares its capabilities,
licensing posture, freshness class, and fallback behaviour. Commands request a
*capability*, never a named provider, so a source can be swapped without
touching evaluation code.

## Classification

| Input | Access | Licence | Status |
|---|---|---|---|
| ESPN league state | Free / public JSON | ESPN terms (see below) | **implemented** |
| nflverse player ID crosswalk | Free / public | CC-BY-4.0 | **implemented** |
| nflverse schedule + kickoff | Free / public | CC-BY-4.0 | **implemented** |
| nflverse betting lines (spread/total) | Free / public | CC-BY-4.0 | **implemented** |
| nflverse weather (roof/temp/wind) | Free / public | CC-BY-4.0 | **implemented** |
| nflverse injuries + practice participation | Free / public | CC-BY-4.0 | **implemented** |
| nflverse depth charts | Free / public | CC-BY-4.0 | **implemented** |
| nflverse snap counts (usage) | Free / public | CC-BY-4.0 | **implemented** |
| FantasyPros ECR (rankings) | Free via DynastyProcess | **GPL-3.0 repo** | optional, flagged |
| **Weekly point projections** | — | — | **not selected** |
| **ADP** | — | — | **not selected** |
| Editorial news | — | — | not selected |

## Adapters

### `espn` — league provider

Publicly readable JSON endpoints used by ESPN's own web client, under
`lm-api-reads.fantasy.espn.com`. Read-only GETs at low volume for a league the
user is a member of. No HTML scraping, no redistribution of payloads.

Private leagues require `espn_s2` and `SWID` cookies from a logged-in browser
session, supplied via environment variables and never logged.

**Capabilities:** league settings, rosters, matchups, transactions, draft, free
agents, player registry, market trends, injuries.

> **Verify scoring before trusting projections.** ESPN publishes no schema for
> its stat identifiers; our map is community consensus. Offensive ids (0–72) are
> well established; kicking (74–88) and defensive (89–135) ids are less
> consistently documented. Run:
>
> ```bash
> draftgpt sources verify-espn
> ```
>
> An unrecognised scoring rule is **never** silently dropped — it is preserved in
> `raw_settings.unmapped_scoring_items` and reported as a warning, because a
> silently ignored rule corrupts every downstream number with no symptom.

### `nflverse` — registry and context provider

Accessed via [`nflreadpy`](https://github.com/nflverse/nflreadpy) (MIT). Data is
[CC-BY-4.0](https://creativecommons.org/licenses/by/4.0/): **attribution is
required** and is emitted in recommendation provenance and in the README.

This one adapter collapses six source categories the brief lists separately,
because `load_schedules()` alone carries schedule, kickoff, spread, total,
moneyline, roof, temperature, and wind.

**Deliberate exclusions:**

- `load_ftn_charting()` is **not** exposed. FTN data is CC-BY-SA-4.0 and
  share-alike obligations should not attach to this project by accident.
- `load_ff_playerids()` and `load_ff_rankings()` proxy DynastyProcess, whose
  repository is GPL-3.0. nflverse's own rosters already carry `espn_id`, so
  these are optional supplements, never load-bearing.

#### Identity model

`gsis_id` is nflverse's anchor. `load_rosters()` carries the bridges we need —
`espn_id`, `sleeper_id`, `yahoo_id`, `pfr_id`, `sportradar_id` — which makes the
roster table the hub of a hub-and-spoke crosswalk. Snap counts key on `pfr_id`
rather than `gsis_id`, so that translation goes through the hub.

**Measured coverage caveat:** `espn_id` is present for **84.4%** of
fantasy-relevant players (885/1049, 2024 rosters). The remaining ~16% — mostly
rookies and practice-squad callups — must still resolve by name. The
conservative name resolver is therefore load-bearing, not vestigial, and
`draftgpt sync league-players` exists specifically to create canonical
identities for players nflverse has no id for.

### `fixture_league`, `fixture_projections` — local fallbacks

Hand-authored JSON and CSV. Three purposes: run the pipeline with no credentials
or network, provide frozen golden-scenario state for tests, and stand in behind
the same interface for a licensed source that cannot yet be integrated.

Projections load from CSV so any source the owner is entitled to use can be
hand-imported without writing code. Any column that is not metadata is treated
as a canonical stat key, so a new scoring category needs no code change.

## Freshness classes

| Class | Examples | Regular season | Game day |
|---|---|---|---|
| `static` | league rules, historical results | 30 d | 30 d |
| `slow` | preseason projections, tiers, ADP | 7 d | 7 d |
| `daily` | weekly projections, depth charts, usage | 24 h | 12 h |
| `rapid` | practice reports, injuries, transactions, lines | 12 h | 2 h |
| `live` | draft picks, inactives, game-day status | 1 h | 15 min |

A command never blocks on a non-critical stale source: it warns, reduces
confidence, and still answers. A stale **critical** source (rosters, league
settings) marks the whole response `stale`.

## What is still needed

**Weekly point projections** and **ADP** are the two genuine gaps. nflverse
gives rankings (ECR) and historical results, but ECR is an ordering, not a
forward-looking point estimate, and nothing here supplies ADP.

Candidate paths, in order of preference:

1. **ESPN's own projections.** Already reachable through the `kona_player_info`
   payload we fetch. Free, league-consistent, and needs no new terms review —
   but they are ESPN's numbers, with ESPN's known biases.
2. **Manual CSV import** from a source the owner subscribes to. Already
   supported today via `fixture_projections`.
3. **A commercial API.** Requires a terms and cost review before an adapter is
   written.

ADP has no free, reliably-licensed source identified yet. Until one exists,
`/draft` survival estimates will need either a manual ADP import or a fallback
to ranking dispersion, which is materially weaker.
