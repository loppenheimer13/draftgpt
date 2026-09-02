# Huddle

A daily sports show for your kid's Yoto player.

Parents pick the teams their family follows — across the NFL, NBA, MLB, NHL,
WNBA, college sports and soccer. Every morning the **same MYO card** plays a
fresh three-to-six minute show, hosted by two consistent voices.

```
01  Your Teams        scores, next game, one thing to know
02  Today in Sports   the biggest kid-friendly stories
03  Birthday Club     notable athletes celebrating today
04  On This Day       a moment worth remembering
05  Rookie Factoid    a rule, a record, a tradition, a word
06  Weekend Edition   what to watch and who to root for  (Thu–Sat)
```

Each segment is its own chapter, so the skip buttons on the player do something
a five-year-old can use.

## What it will not say

This is the part that matters, and it is enforced in code rather than promised
in a README. **No betting or odds. No arrests, lawsuits or scandals. No
injuries described in detail. No transfer fees, contract disputes or tabloid
sourcing. No adult fantasy-sports analysis.**

A story reaches the microphone only if it clears four gates:

1. **Type** — the provider's own classification is one we air.
2. **Block list** — no blocked phrase anywhere in headline, summary or tags.
3. **Shape** — it must *positively* match something a children's show airs: a
   game result, a milestone, a debut, an explainer.
4. **Final check** — the finished script is re-scanned after it is written, and
   again after any model rewrite.

Gate 3 is what makes the filter sound rather than merely long. A block list
alone always loses, because sports feeds invent new adult phrasings faster than
anyone maintains a word list — during development this filter's block list
happily passed a drunk-driving plea and a contract stalemate, simply because
nobody had thought to add those words yet. Requiring a recognised safe shape
turns every unanticipated story into a silent drop instead of a broadcast.

Roughly two thirds of a live sports feed is filtered out. That is the intended
ratio, not a bug.

Every parent can see exactly what was removed and why, by name, on the
**Safety** page — because a claim about children's content should be checkable,
not merely believed.

## How it stays honest

- **Nothing is invented.** The show reads facts from a database that records
  where each one came from and when. A team with no result yet is described as
  having no result yet, never as nothing–nothing.
- **The narration model may not add a fact.** It is off by default. When it is
  on, it receives finished copy — never the raw data — and any rewrite that
  introduces a number or a proper noun the source did not contain is discarded
  automatically, and the written copy airs instead.
- **Stale data is announced.** If the scores came in late, a host says so.
- **Ambiguity is left unsaid.** A three-part soccer record is win–draw–loss and
  a hockey one is win–loss–overtime; rather than guess, the show talks about
  the standing instead.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,web]"

cp .env.example .env          # set HUDDLE_YOTO_CLIENT_ID at minimum
alembic upgrade head

huddle sync teams             # fill the team picker
huddle serve                  # http://127.0.0.1:8787
```

Open the app, connect your Yoto account, and pick some teams. Then:

```bash
huddle sync followed          # scores, news and rosters for followed teams
huddle show build all         # write today's show and print it
huddle show build all --publish   # …and send it to the card
```

To run it daily, put this on a cron:

```bash
huddle sync followed && huddle show daily
```

`show daily` builds for each family whose local publish hour has arrived, and
skips any whose show would be identical to yesterday's.

## Getting a Yoto app

Create one at [dashboard.yoto.dev](https://dashboard.yoto.dev). Register
`http://127.0.0.1:8787/auth/callback` as a redirect URL, and use a **public
client** — Huddle uses PKCE and needs no client secret.

Huddle requests exactly three scopes:

| Scope | Why |
|---|---|
| `profile` | to know which Yoto account this is |
| `offline_access` | so the show can publish each morning unattended |
| `user:content:manage` | to create and update its own card |

It deliberately does **not** request any device scope. Huddle publishes audio;
it never controls a player, changes a setting, or touches content it did not
create. That is enforced by construction — the API client has no method that
could.

No browser on the box? `huddle auth device` runs the device-code flow instead.

## Audio

Two pipelines, both speaking [ElevenLabs](https://elevenlabs.io) voice ids.

**`labs`** (default) posts the script to Yoto's Labs API and lets Yoto
synthesise it. No audio files, no extra API key, no upload or transcode step.
Each track carries its own `voiceId`, which is what lets two hosts alternate.
Limit: 3000 characters per track, which the writer respects.

**`upload`** synthesises locally, uploads the audio, waits for Yoto's
transcoder, and points each track at the resulting content hash. More control
over voice and length; another API key and more moving parts. Set
`HUDDLE_AUDIO_PIPELINE=upload` and `HUDDLE_ELEVENLABS_API_KEY`.

## The hosts

**Coach Nova** carries the facts and the explanations. **Rookie Rae** carries
the energy and asks the question a listening kid is already thinking.

Yoto synthesises one voice per track, so the track boundary *is* the turn
boundary: each track ends with a handoff in the current host's voice and the
next opens in the other's. That is how real radio hands over, and it costs
nothing to produce.

## Architecture

```
catalog.py       the fourteen leagues; one row per league, nothing else knows the sport
domain/
  safety.py      the four-gate kid-safety filter — the editorial spine
  freshness.py   how stale an input may be before the show says so
providers/       source adapters behind a capability contract (espn, fixture)
ingestion/       tracked fetches, so freshness is always answerable
content/         hand-written On This Day and Rookie Factoid entries, plus the
                 editable safety policy
show/
  brief.py       gathers every fact the episode may state, keyed and sourced
  writer.py      turns facts into broadcast copy: hosts, transitions, runtime
  narration.py   optional model rewrite, with a guardrail that rejects new facts
  runner.py      brief → write → safety re-check → persist → publish
yoto/            OAuth (PKCE + device code), content API, Labs TTS, publishing
web/             the parent app: sign in, pick teams, settings, safety, transcript
```

The separation that matters is **brief → writer**. The brief decides what is
true; the writer decides how it sounds. Nothing downstream of the brief may
introduce a fact, which is what makes the optional narration model safe to
enable at all.

## Running time

`target_minutes` is a **ceiling, not a quota**. The writer budgets characters
per segment and trims the least important material to fit. It never pads in the
other direction: on a genuinely quiet day the show is simply shorter, because
there is no honest way to invent sport. The curated segments — On This Day and
Rookie Factoid — are what stop a quiet day from producing a thirty-second card.

## Data

Scores, schedules, teams, rosters and candidate headlines come from ESPN's
publicly readable JSON endpoints — the same ones its own web client calls —
read once a day per followed league. Huddle stores only the facts it narrates.
It does not scrape HTML, does not redistribute payloads, and writes nothing
back. Article text is never republished: a headline is a *candidate*, and the
broadcast copy is written from scratch.

On This Day and Rookie Factoid are hand-written and versioned with the code.
Nothing in `content/` comes from an API, which is what lets the show carry two
guaranteed-safe, guaranteed-interesting segments even when every league in the
catalogue is out of season.

## Development

```bash
pytest                    # 123 tests
ruff check .
huddle safety audit       # see what the filter would do with today's real headlines
huddle safety test "some headline"
```

`huddle safety audit` is the one to run after touching the filter. It pulls
current headlines from six leagues and prints what would air and what would be
dropped, with reasons.

## Licence

Not yet selected.
