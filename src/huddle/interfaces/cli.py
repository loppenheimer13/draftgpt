"""Command line for operators.

The parent's interface is the web app; this is for whoever runs the service.
Everything the scheduler does is available here as a command, so a daily run
can be debugged by hand without waiting for a cron.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import func, select

from huddle.catalog import LEAGUES, get_league
from huddle.config import get_settings
from huddle.database.models import Family, FavoriteTeam, ShowRun, Team
from huddle.database.session import session_scope
from huddle.domain.safety import SafetyFilter
from huddle.ingestion.sync import (
    followed_league_keys,
    sync_news,
    sync_rosters,
    sync_scoreboard,
    sync_team_details,
    sync_teams,
)

app = typer.Typer(help="Huddle — a daily kids' sportscast for Yoto players.", no_args_is_help=True)
sync_app = typer.Typer(help="Pull league data.")
show_app = typer.Typer(help="Build and publish shows.")
family_app = typer.Typer(help="Inspect families and their teams.")
safety_app = typer.Typer(help="Check the kid-safety filter.")
auth_app = typer.Typer(help="Connect a Yoto account from the terminal.")
app.add_typer(sync_app, name="sync")
app.add_typer(show_app, name="show")
app.add_typer(family_app, name="family")
app.add_typer(safety_app, name="safety")
app.add_typer(auth_app, name="auth")

console = Console()


@app.callback()
def _root(verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )


# -- leagues ---------------------------------------------------------------
@app.command("leagues")
def leagues_cmd() -> None:
    """List every league families can follow."""
    table = Table("key", "league", "group", "in season now", "spoken as")
    month = datetime.now(UTC).month
    for spec in LEAGUES:
        table.add_row(
            spec.key,
            spec.display_name,
            str(spec.group),
            "yes" if spec.in_season(month) else "no",
            spec.spoken_name,
        )
    console.print(table)


# -- sync ------------------------------------------------------------------
def _league_list(leagues: str | None) -> list[str] | None:
    if not leagues:
        return None
    return [get_league(key.strip()).key for key in leagues.split(",") if key.strip()]


@sync_app.command("teams")
def sync_teams_cmd(
    leagues: Annotated[str, typer.Option(help="Comma-separated league keys")] = "",
) -> None:
    """Refresh the team lists that fill the parent's picker."""
    with session_scope() as session:
        report = sync_teams(session, _league_list(leagues))
    for line in report.lines():
        console.print(line)


@sync_app.command("scores")
def sync_scores_cmd(
    leagues: Annotated[str, typer.Option()] = "",
    on: Annotated[str, typer.Option(help="YYYY-MM-DD, defaults to today")] = "",
) -> None:
    """Pull today's scoreboard for each league."""
    day = date.fromisoformat(on) if on else None
    with session_scope() as session:
        keys = _league_list(leagues) or followed_league_keys(session) or None
        report = sync_scoreboard(session, keys, on=day)
    for line in report.lines():
        console.print(line)


@sync_app.command("news")
def sync_news_cmd(leagues: Annotated[str, typer.Option()] = "") -> None:
    """Pull candidate stories and record a safety verdict for each."""
    with session_scope() as session:
        keys = _league_list(leagues) or followed_league_keys(session) or None
        report = sync_news(session, keys)
    for line in report.lines():
        console.print(line)


@sync_app.command("followed")
def sync_followed_cmd() -> None:
    """Refresh everything the currently followed teams need.

    This is the command a daily cron should run before building shows.
    """
    with session_scope() as session:
        team_ids = [
            row[0] for row in session.execute(select(FavoriteTeam.team_id).distinct())
        ]
        keys = followed_league_keys(session) or None
        console.print(
            f"[bold]{len(team_ids)}[/bold] followed teams "
            f"across {len(keys or [])} leagues"
        )
        for report in (
            sync_scoreboard(session, keys),
            sync_news(session, keys),
            sync_team_details(session, team_ids),
            sync_rosters(session, team_ids),
        ):
            for line in report.lines():
                console.print("  " + line)


# -- show ------------------------------------------------------------------
@show_app.command("build")
def show_build(
    family_id: Annotated[str, typer.Argument(help="Family id, or 'all'")] = "all",
    publish: Annotated[bool, typer.Option(help="Send to the Yoto card")] = False,
    show_text: Annotated[bool, typer.Option("--text", help="Print the script")] = True,
) -> None:
    """Write today's show. Add --publish to send it to the card."""
    from huddle.show.runner import build_show, publish_for_family

    settings = get_settings()
    with session_scope() as session:
        stmt = select(Family).where(Family.active.is_(True))
        if family_id != "all":
            stmt = stmt.where(Family.id == family_id)
        families = list(session.scalars(stmt))
        if not families:
            console.print("[yellow]no families found[/yellow]")
            raise typer.Exit(1)

        for family in families:
            result = build_show(session, family, force=True, settings=settings)
            console.print(
                f"\n[bold]{family.display_name or family.id[:8]}[/bold] — "
                f"{result.run.show_date}, about {result.estimated_minutes} minutes, "
                f"{len(result.run.segments)} chapters"
            )
            for warning in result.warnings:
                console.print(f"  [yellow]{warning}[/yellow]")
            if show_text:
                for row in result.run.segments:
                    console.print(f"\n  [cyan]{row.title}[/cyan] ({row.host})")
                    console.print(f"  {row.script}")
            if publish:
                outcome = publish_for_family(session, family, result.run, settings=settings)
                colour = "green" if outcome.ok else "red"
                console.print(
                    f"  [{colour}]{outcome.status}[/{colour}]"
                    f"{' — ' + outcome.error if outcome.error else ''}"
                )


@show_app.command("daily")
def show_daily(
    force: Annotated[bool, typer.Option(help="Ignore each family's publish hour")] = False,
    publish: Annotated[bool, typer.Option()] = True,
) -> None:
    """Build and publish for every family whose local publish hour has arrived."""
    from huddle.show.runner import run_daily

    with session_scope() as session:
        outcomes = run_daily(session, force=force, publish=publish)

    if not outcomes:
        console.print("nothing due right now")
        return
    table = Table("family", "date", "minutes", "chapters", "published")
    for family, build, published in outcomes:
        table.add_row(
            (family.display_name or family.id)[:24],
            str(build.run.show_date) if build.run else "—",
            f"{build.estimated_minutes}" if build.segments else "—",
            str(len(build.run.segments)) if build.run else "—",
            published.status if published else ("unchanged" if build.unchanged else "not sent"),
        )
    console.print(table)


@show_app.command("last")
def show_last(family_id: str) -> None:
    """Print the most recent show for one family."""
    with session_scope() as session:
        run = session.scalar(
            select(ShowRun)
            .where(ShowRun.family_id == family_id)
            .order_by(ShowRun.show_date.desc())
            .limit(1)
        )
        if run is None:
            console.print("[yellow]no shows built for that family[/yellow]")
            raise typer.Exit(1)
        console.print(f"[bold]{run.show_date}[/bold] — {run.status}")
        for row in run.segments:
            console.print(f"\n[cyan]{row.title}[/cyan] ({row.host})\n{row.script}")


# -- families --------------------------------------------------------------
@family_app.command("list")
def family_list() -> None:
    with session_scope() as session:
        table = Table("id", "name", "timezone", "hour", "teams", "last published")
        for family in session.scalars(select(Family)):
            count = session.scalar(
                select(func.count()).select_from(FavoriteTeam).where(
                    FavoriteTeam.family_id == family.id
                )
            )
            table.add_row(
                family.id[:12],
                family.display_name or "—",
                family.timezone,
                str(family.publish_hour),
                str(count or 0),
                str(family.last_published_at or "never"),
            )
        console.print(table)


@family_app.command("follow")
def family_follow(family_id: str, league: str, team_query: str) -> None:
    """Follow a team by name, for setting up or debugging without a browser."""
    spec = get_league(league)
    with session_scope() as session:
        family = session.get(Family, family_id)
        if family is None:
            console.print("[red]no such family[/red]")
            raise typer.Exit(1)
        matches = list(
            session.scalars(
                select(Team).where(
                    Team.league_key == spec.key,
                    Team.search_name.contains(team_query.lower()),
                )
            )
        )
        if not matches:
            console.print(f"[yellow]no {spec.display_name} team matching {team_query!r}[/yellow]")
            raise typer.Exit(1)
        if len(matches) > 1:
            # Refuse rather than guess: following the wrong team is silent and
            # only noticed when tomorrow's show is about strangers.
            console.print("[yellow]ambiguous — be more specific:[/yellow]")
            for team in matches[:10]:
                console.print(f"  {team.display_name}")
            raise typer.Exit(1)

        team = matches[0]
        session.add(FavoriteTeam(family_id=family.id, team_id=team.id))
        session.flush()
        sync_team_details(session, [team.id])
        sync_rosters(session, [team.id])
        console.print(f"[green]{family.display_name or family.id[:8]} now follows "
                      f"{team.display_name}[/green]")


# -- safety ----------------------------------------------------------------
@safety_app.command("test")
def safety_test(
    headline: str,
    summary: Annotated[str, typer.Option()] = "",
    story_type: Annotated[str, typer.Option()] = "story",
) -> None:
    """Check whether a headline would reach the show, and why."""
    result = SafetyFilter().check_story(
        {"headline": headline, "summary": summary or headline, "type": story_type}
    )
    if result.allowed:
        console.print(f"[green]ALLOWED[/green] as a '{result.shape}' story"
                      f"{' (caution: not as the lead)' if result.caution else ''}")
    else:
        console.print(
            f"[red]BLOCKED[/red] — {result.category}"
            f"{': matched ' + repr(result.matched) if result.matched else ''}"
        )


@safety_app.command("audit")
def safety_audit(leagues: Annotated[str, typer.Option()] = "", limit: int = 40) -> None:
    """Show what the filter would do with current headlines, side by side."""
    from huddle.providers.registry import build as build_provider

    provider = build_provider("espn")
    safety = SafetyFilter()
    keys = _league_list(leagues) or [s.key for s in LEAGUES[:6]]

    allowed_total = seen_total = 0
    for key in keys:
        try:
            result = provider.fetch("league_news", league=key, limit=limit)
        except Exception as exc:  # noqa: BLE001
            console.print(f"[yellow]{key}: {exc}[/yellow]")
            continue
        allowed, blocked = safety.filter_stories(result.records)
        seen_total += len(result.records)
        allowed_total += len(allowed)
        console.print(f"\n[bold]{key}[/bold] — {len(allowed)} of {len(result.records)} would air")
        for story in allowed[:6]:
            console.print(f"  [green]✓[/green] [{story['shape']}] {story['headline'][:72]}")
        for item in blocked[:4]:
            console.print(f"  [red]✗[/red] [{item['category']}] {(item['headline'] or '')[:72]}")
    console.print(f"\n[bold]{allowed_total} of {seen_total}[/bold] stories would reach a show")


# -- auth ------------------------------------------------------------------
@auth_app.command("device")
def auth_device() -> None:
    """Connect a Yoto account from a machine with no browser."""
    from huddle.web.auth import upsert_family
    from huddle.yoto.auth import DeviceCodeFlow, YotoAuthError, store_tokens

    settings = get_settings()
    try:
        flow = DeviceCodeFlow(settings)
        started = flow.start()
    except YotoAuthError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    console.print(
        f"\nOpen [bold]{started.get('verification_uri_complete') or started['verification_uri']}"
        f"[/bold]\nand enter the code [bold]{started['user_code']}[/bold]\n"
    )
    try:
        tokens = flow.poll(
            started["device_code"],
            interval=int(started.get("interval", 5)),
            expires_in=int(started.get("expires_in", 600)),
        )
    except YotoAuthError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    with session_scope() as session:
        family = upsert_family(session, tokens)
        store_tokens(family.credential, tokens)
        console.print(f"[green]connected[/green] — family id {family.id}")


@app.command("serve")
def serve(
    host: str = "127.0.0.1",
    port: int = 8787,
    reload: bool = False,
) -> None:
    """Run the parent-facing web app."""
    try:
        import uvicorn
    except ImportError as exc:
        console.print('[red]install the web extra: pip install -e ".[web]"[/red]')
        raise typer.Exit(1) from exc
    uvicorn.run("huddle.web.app:app", host=host, port=port, reload=reload)
