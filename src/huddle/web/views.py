"""HTML for the parent app.

Rendered from plain functions rather than templates: there are seven pages, no
designer, and no build step, so a template engine would add a directory to keep
in sync for no benefit. Everything user-supplied goes through :func:`esc`.
"""

from __future__ import annotations

from html import escape

STYLE = """
:root {
  color-scheme: light dark;
  --bg: #fbfaf7; --fg: #1b1b1f; --muted: #5f6470;
  --line: #e2e0da; --card: #ffffff; --accent: #1f6f5c; --warn: #8a4b12;
}
@media (prefers-color-scheme: dark) {
  :root { --bg:#14151a; --fg:#f2f1ee; --muted:#a3a7b3; --line:#2b2d36;
          --card:#1c1e25; --accent:#5fc0a4; --warn:#e0a35f; }
}
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg);
  font:16px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; }
.wrap { max-width: 860px; margin: 0 auto; padding: 24px 20px 72px; }
header { display:flex; align-items:center; justify-content:space-between;
  gap:16px; padding-bottom:16px; border-bottom:1px solid var(--line); margin-bottom:28px; }
.brand { font-weight:700; font-size:20px; letter-spacing:-0.01em; }
.brand span { color:var(--accent); }
nav a { color:var(--muted); text-decoration:none; margin-left:16px; font-size:14px; }
nav a:hover, nav a.on { color:var(--fg); }
h1 { font-size:26px; letter-spacing:-0.02em; margin:0 0 8px; }
h2 { font-size:17px; margin:32px 0 12px; }
p.lede { color:var(--muted); margin:0 0 24px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:12px;
  padding:18px 20px; margin-bottom:14px; }
.row { display:flex; justify-content:space-between; align-items:center; gap:12px; }
.muted { color:var(--muted); font-size:14px; }
.tag { display:inline-block; font-size:12px; padding:2px 8px; border-radius:999px;
  border:1px solid var(--line); color:var(--muted); }
.btn { display:inline-block; background:var(--accent); color:#fff; border:0;
  border-radius:8px; padding:10px 16px; font-size:15px; font-weight:600;
  cursor:pointer; text-decoration:none; }
.btn.ghost { background:transparent; color:var(--fg); border:1px solid var(--line); }
.btn.small { padding:6px 12px; font-size:13px; }
.grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(210px,1fr)); gap:10px; }
.team { display:flex; align-items:center; gap:10px; border:1px solid var(--line);
  background:var(--card); border-radius:10px; padding:10px 12px; width:100%;
  text-align:left; cursor:pointer; color:var(--fg); font-size:14px; }
.team.on { border-color:var(--accent); box-shadow:inset 0 0 0 1px var(--accent); }
.team .dot { width:10px; height:10px; border-radius:50%; flex:0 0 auto; }
.leagues { display:flex; flex-wrap:wrap; gap:8px; margin-bottom:8px; }
.leagues a { font-size:13px; padding:6px 12px; border-radius:999px;
  border:1px solid var(--line); text-decoration:none; color:var(--muted); }
.leagues a.on { background:var(--fg); color:var(--bg); border-color:var(--fg); }
label { display:block; font-size:14px; margin:14px 0 4px; }
input[type=text], input[type=number], select { width:100%; padding:9px 11px;
  border:1px solid var(--line); border-radius:8px; background:var(--bg);
  color:var(--fg); font-size:15px; }
.check { display:flex; align-items:center; gap:10px; margin:8px 0; font-size:15px; }
.blocked { border-left:3px solid var(--warn); padding-left:12px; margin:10px 0; }
.blocked code { color:var(--warn); font-size:13px; }
.script { white-space:pre-wrap; line-height:1.65; }
.host { font-size:12px; text-transform:uppercase; letter-spacing:.06em;
  color:var(--accent); font-weight:700; }
footer { margin-top:48px; padding-top:16px; border-top:1px solid var(--line);
  color:var(--muted); font-size:13px; }
"""


def esc(value: object) -> str:
    return escape(str(value if value is not None else ""))


def page(title: str, body: str, *, active: str = "", signed_in: bool = True) -> str:
    nav = ""
    if signed_in:
        links = [("/", "Home"), ("/teams", "Teams"), ("/show", "Today's show"),
                 ("/safety", "Safety"), ("/settings", "Settings"), ("/logout", "Sign out")]
        nav = "".join(
            f'<a href="{href}" class="{"on" if active == href else ""}">{esc(label)}</a>'
            for href, label in links
        )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} — Huddle</title><style>{STYLE}</style></head>
<body><div class="wrap">
<header><div class="brand">Hud<span>dle</span></div><nav>{nav}</nav></header>
{body}
<footer>Huddle publishes one card a day to your Yoto player. It never controls
your player and never posts anything anywhere.</footer>
</div></body></html>"""


def landing() -> str:
    body = """
<h1>A daily sports show for your Yoto player</h1>
<p class="lede">Pick the teams your family follows across the NFL, NBA, MLB, NHL,
WNBA, college sports and soccer. Every morning, the same card plays a fresh
three-to-six minute show: your teams' scores, the day's biggest kid-friendly
stories, birthdays, a moment from history and one thing worth knowing.</p>
<div class="card">
  <p style="margin-top:0"><strong>No betting. No scandals. No injuries in
  detail. No grown-up sports-talk noise.</strong> Every story is checked against
  a safety filter before it reaches the microphone, and you can read exactly
  what was filtered out and why.</p>
  <a class="btn" href="/login">Connect your Yoto account</a>
</div>
<p class="muted">Signing in uses Yoto's own login. Huddle never sees your
password, and asks only for permission to create and update its own card.</p>
"""
    return page("A daily sports show", body, signed_in=False)


def error_page(title: str, detail: str) -> str:
    body = f"""<h1>{esc(title)}</h1><p class="lede">{esc(detail)}</p>
<a class="btn ghost" href="/">Back to the start</a>"""
    return page(title, body, signed_in=False)


def dashboard(family, favourites, latest_run) -> str:
    name = esc(family.display_name or "there")
    if favourites:
        teams_html = "".join(
            f'<div class="card row"><div><strong>{esc(team.display_name)}</strong>'
            f'<div class="muted">{esc(_league_name(team.league_key))}'
            f'{" · " + esc(team.record_summary) if team.record_summary else ""}</div></div>'
            f'<span class="tag">{esc(team.abbreviation or "")}</span></div>'
            for _, team in favourites
        )
    else:
        teams_html = (
            '<div class="card"><p style="margin:0">No teams yet. '
            '<a href="/teams">Pick some</a> and tomorrow\'s show is about them.</p></div>'
        )

    if latest_run is not None:
        status = esc(latest_run.status)
        minutes = latest_run.estimated_minutes or 0
        run_html = (
            f'<div class="card"><div class="row"><div>'
            f'<strong>{esc(latest_run.show_date)}</strong>'
            f'<div class="muted">{status} · about {minutes:.0f} minutes · '
            f'{len(latest_run.segments)} chapters</div></div>'
            f'<a class="btn ghost small" href="/show">Read it</a></div></div>'
        )
    else:
        run_html = '<div class="card"><p style="margin:0">No show built yet.</p></div>'

    body = f"""
<h1>Good to see you, {name}</h1>
<p class="lede">Your show is published to the same Yoto card every morning at
{family.publish_hour}:00, {esc(family.timezone)}.</p>
<h2>Latest show</h2>
{run_html}
<form method="post" action="/show/build" style="margin:12px 0 8px">
  <button class="btn" name="publish" value="1">Build and send to my card now</button>
  <button class="btn ghost" name="publish" value="">Build a preview only</button>
</form>
<h2>Teams you follow</h2>
{teams_html}
<a class="btn ghost" href="/teams">Add or remove teams</a>
"""
    return page("Home", body, active="/")


def teams_page(family, spec, teams, chosen: set, groups, query: str) -> str:
    league_links = ""
    for group, specs in groups.items():
        label = {"pro_us": "Pro", "college": "College", "soccer": "Soccer"}.get(
            str(group), str(group)
        )
        chips = "".join(
            f'<a href="/teams?league={s.key}" class="{"on" if s.key == spec.key else ""}">'
            f"{esc(s.display_name)}</a>"
            for s in specs
        )
        league_links += f'<p class="muted" style="margin:14px 0 6px">{label}</p>' \
                        f'<div class="leagues">{chips}</div>'

    cards = "".join(
        f'<form method="post" action="/teams/follow">'
        f'<input type="hidden" name="team_id" value="{esc(team.id)}">'
        f'<input type="hidden" name="league" value="{esc(spec.key)}">'
        f'<button class="team {"on" if team.id in chosen else ""}" type="submit">'
        f'<span class="dot" style="background:#{esc(team.color or "999999")}"></span>'
        f'<span>{esc(team.display_name)}</span></button></form>'
        for team in teams
    )
    if not cards:
        cards = '<p class="muted">No teams matched. Try a different search.</p>'

    search_note = (
        '<p class="muted">This competition has a lot of teams, so use the search box.</p>'
        if spec.large_roster else ""
    )
    body = f"""
<h1>Pick your teams</h1>
<p class="lede">{esc(spec.blurb)} Tap a team to follow or unfollow it.</p>
{league_links}
<form method="get" action="/teams" style="margin:18px 0">
  <input type="hidden" name="league" value="{esc(spec.key)}">
  <input type="text" name="q" value="{esc(query)}" placeholder="Search teams…">
</form>
{search_note}
<div class="grid">{cards}</div>
"""
    return page("Teams", body, active="/teams")


def settings_page(family, segment_order, segment_titles) -> str:
    checks = "".join(
        f'<label class="check"><input type="checkbox" name="segment_{esc(name)}" '
        f'{"checked" if (not family.segments or name in family.segments) else ""}> '
        f"{esc(segment_titles.get(name, name))}</label>"
        for name in segment_order
    )
    body = f"""
<h1>Settings</h1>
<p class="lede">How long the show runs, when it lands, and who it's for.</p>
<form method="post" action="/settings" class="card">
  <label>Time zone
    <input type="text" name="timezone" value="{esc(family.timezone)}"></label>
  <label>Publish at (hour, 0–23)
    <input type="number" name="publish_hour" min="0" max="23"
           value="{family.publish_hour}"></label>
  <label>Target length in minutes (3–10)
    <input type="number" name="target_minutes" min="3" max="10"
           value="{family.target_minutes}"></label>
  <label>Listener's age (3–16)
    <input type="number" name="listener_age" min="3" max="16"
           value="{family.listener_age}"></label>
  <p class="muted">Age decides how much the hosts explain, not what they cover.
  Everything in the show is written for children either way.</p>
  <button class="btn" type="submit">Save</button>
</form>
<h2>Segments</h2>
<form method="post" action="/settings/segments" class="card">
  {checks}
  <p class="muted">Weekend Edition only airs Thursday to Saturday. Turning a
  segment off makes the others longer rather than shortening the show.</p>
  <button class="btn" type="submit">Save segments</button>
</form>
"""
    return page("Settings", body, active="/settings")


def safety_page(family, latest_run) -> str:
    blocked = (latest_run.blocked_stories if latest_run else []) or []
    if blocked:
        items = "".join(
            f'<div class="blocked"><div>{esc(item.get("headline"))}</div>'
            f'<code>blocked: {esc(item.get("reason"))}'
            f'{" · matched “" + esc(item.get("matched")) + "”" if item.get("matched") else ""}'
            f"</code></div>"
            for item in blocked[:60]
        )
    else:
        items = '<p class="muted">Nothing was filtered out of the latest show.</p>'

    body = f"""
<h1>What we left out</h1>
<p class="lede">Every story is checked before it can reach the show. These are
the ones the filter removed while building your most recent episode, and the
reason each was removed.</p>
<div class="card">
<p style="margin-top:0">A story has to clear four gates to air: an allowed
story type, no blocked phrase anywhere in it, a recognised safe shape (a
result, a milestone, a debut, an explainer), and a final check on the finished
script after it has been written. Anything unrecognised is dropped rather
than guessed at.</p>
<p class="muted" style="margin-bottom:0">Blocked topics include betting and
odds, legal and police matters, substances, injuries described in detail,
death and harm, transfers and contracts, tabloid sourcing, and adult
fantasy-sports analysis.</p>
</div>
<h2>Filtered from the latest show ({len(blocked)})</h2>
{items}
"""
    return page("Safety", body, active="/safety")


def transcript_page(family, latest_run) -> str:
    if latest_run is None or not latest_run.segments:
        body = """<h1>Today's show</h1>
<p class="lede">Nothing has been built yet.</p>
<form method="post" action="/show/build">
  <button class="btn" name="publish" value="">Build a preview now</button></form>"""
        return page("Today's show", body, active="/show")

    from huddle.domain.enums import HOST_PROFILES

    chapters = "".join(
        f'<div class="card"><div class="row"><strong>{esc(row.title)}</strong>'
        f'<span class="host">{esc(HOST_PROFILES.get(row.host, {}).get("name", row.host))}'
        f"</span></div>"
        f'<p class="script">{esc(row.script)}</p></div>'
        for row in latest_run.segments
    )
    minutes = latest_run.estimated_minutes or 0
    narration_note = " · narration model used" if latest_run.narration_used else ""
    warnings = ""
    if latest_run.warnings:
        warnings = '<div class="card"><strong>Notes</strong><ul class="muted">' + "".join(
            f"<li>{esc(w)}</li>" for w in latest_run.warnings
        ) + "</ul></div>"

    body = f"""
<h1>Show for {esc(latest_run.show_date)}</h1>
<p class="lede">{esc(latest_run.status)} · about {minutes:.0f} minutes ·
{len(latest_run.segments)} chapters{narration_note}</p>
{warnings}
{chapters}
<form method="post" action="/show/build" style="margin-top:16px">
  <button class="btn" name="publish" value="1">Rebuild and send to my card</button>
</form>
"""
    return page("Today's show", body, active="/show")


def _league_name(key: str) -> str:
    from huddle.catalog import LEAGUES_BY_KEY

    spec = LEAGUES_BY_KEY.get(key)
    return spec.display_name if spec else key
