"""Command-line interface.

Handlers are deliberately thin: they resolve arguments, call the engine, and
render. All decision logic lives in ``commands/`` and ``evaluation/`` so the
same calls back an MCP server, an HTTP API, or a chat client unchanged.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from draftgpt.config import get_settings
from draftgpt.database.models import League, LeagueSeason, Team, TeamSeason
from draftgpt.database.session import get_engine, session_scope
from draftgpt.domain.contract import CommandResponse

app = typer.Typer(
    help="League-specific fantasy football decision engine (read-only).",
    no_args_is_help=True,
    add_completion=False,
)
sources_app = typer.Typer(
    help="Provider adapters: register, inspect, health.", no_args_is_help=True
)
sync_app = typer.Typer(help="Ingest external state into the database.", no_args_is_help=True)
league_app = typer.Typer(help="League configuration.", no_args_is_help=True)
roster_app = typer.Typer(help="Lineup recommendations.", no_args_is_help=True)

prep_app = typer.Typer(
    help="Pre-draft analysis: league strategy and draft board.", no_args_is_help=True
)

app.add_typer(sources_app, name="sources")
app.add_typer(sync_app, name="sync")
app.add_typer(league_app, name="league")
app.add_typer(roster_app, name="roster")
app.add_typer(prep_app, name="prep")

console = Console()


@app.callback()
def _root(verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False) -> None:
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )


# --------------------------------------------------------------------------
# sources
# --------------------------------------------------------------------------
@sources_app.command("register")
def sources_register() -> None:
    """Register every known adapter into ``source_providers``."""
    from draftgpt.ingestion.runner import sync_provider_registry
    from draftgpt.providers.registry import all_specs

    with session_scope() as session:
        providers = sync_provider_registry(session, all_specs())
        console.print(f"[green]Registered {len(providers)} providers.[/green]")


@sources_app.command("list")
def sources_list() -> None:
    """Show adapters, their capabilities, and licensing posture."""
    from draftgpt.providers.registry import all_specs

    table = Table(title="Provider adapters", show_lines=True)
    table.add_column("Key", style="cyan", no_wrap=True)
    table.add_column("Freshness")
    table.add_column("Auth")
    table.add_column("Capabilities")
    table.add_column("Licence")

    for spec in all_specs():
        table.add_row(
            spec.key,
            str(spec.freshness_class),
            "yes" if spec.requires_auth else "no",
            ", ".join(str(c) for c in spec.capabilities),
            spec.license_note[:90] + ("..." if len(spec.license_note) > 90 else ""),
        )
    console.print(table)


@sources_app.command("health")
def sources_health() -> None:
    """Probe each adapter and report reachability."""
    from draftgpt.providers.registry import available_keys, build

    table = Table(title="Adapter health")
    table.add_column("Provider", style="cyan")
    table.add_column("OK")
    table.add_column("Detail")
    for key in available_keys():
        try:
            ok, detail = build(key).health_check()
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, str(exc)
        table.add_row(key, "[green]yes[/green]" if ok else "[red]no[/red]", detail[:100])
    console.print(table)


@sources_app.command("freshness")
def sources_freshness() -> None:
    """Current freshness state for every ingested capability."""
    from draftgpt.ingestion.runner import collect_freshness

    with session_scope() as session:
        rows = collect_freshness(session)
    if not rows:
        console.print("[yellow]No ingestion has run yet.[/yellow]")
        return
    table = Table(title="Source freshness")
    table.add_column("Provider", style="cyan")
    table.add_column("Capability")
    table.add_column("Class")
    table.add_column("Last success")
    table.add_column("Health")
    for row in rows:
        table.add_row(
            row.provider,
            row.capability,
            row.freshness_class,
            row.last_success_at.isoformat(timespec="seconds") if row.last_success_at else "never",
            row.health,
        )
    console.print(table)


@sources_app.command("verify-espn")
def sources_verify_espn() -> None:
    """Dump the league's raw ESPN scoring items beside our normalization.

    ESPN publishes no schema for stat ids. Run this once per league before
    trusting any projection: an unmapped or mis-mapped scoring rule silently
    corrupts every downstream number.
    """
    from draftgpt.providers.espn.adapter import _normalize_scoring
    from draftgpt.providers.registry import build

    settings = get_settings()
    if not settings.espn_league_id:
        console.print("[red]DRAFTGPT_ESPN_LEAGUE_ID is not set.[/red]")
        raise typer.Exit(1)

    provider = build("espn")
    raw = provider.client.get(["mSettings"])  # type: ignore[attr-defined]
    items = ((raw.get("settings") or {}).get("scoringSettings") or {}).get("scoringItems", [])
    scoring, unmapped = _normalize_scoring(items)

    table = Table(title="ESPN scoring normalization")
    table.add_column("Canonical key", style="cyan")
    table.add_column("Points")
    for key, value in sorted(scoring.items()):
        if isinstance(value, list):
            table.add_row(key, f"{len(value)} tier(s)")
        else:
            table.add_row(key, str(value))
    console.print(table)

    if unmapped:
        console.print(
            f"\n[red]{len(unmapped)} scoring item(s) have no canonical mapping "
            "and are NOT being scored:[/red]"
        )
        for item in unmapped:
            console.print(f"  statId={item['stat_id']}  points={item['points']}")
        console.print(
            "\n[yellow]Add these to STAT_BY_ID in providers/espn/constants.py "
            "before relying on projections.[/yellow]"
        )
    else:
        console.print("\n[green]All scoring items mapped.[/green]")


# --------------------------------------------------------------------------
# sync
# --------------------------------------------------------------------------
@sync_app.command("players")
def sync_players(
    season: Annotated[int | None, typer.Option(help="Season to load rosters for.")] = None,
) -> None:
    """Build the canonical player registry and cross-platform ID crosswalk."""
    from draftgpt.ingestion.registry_sync import sync_player_registry

    settings = get_settings()
    season = season or settings.season
    with session_scope() as session:
        report = sync_player_registry(session, season)
    console.print(f"[green]{report.summary()}[/green]")
    for note in report.notes:
        console.print(f"  [dim]{note}[/dim]")


@sync_app.command("league-players")
def sync_league_players(
    provider: Annotated[str | None, typer.Option(help="Adapter key.")] = None,
    season: Annotated[int | None, typer.Option()] = None,
    create_missing: Annotated[bool, typer.Option(help="Create players nflverse lacks.")] = True,
) -> None:
    """Reconcile the league platform's player pool into the canonical registry.

    Run this after `sync players`. It covers the players nflverse has no id for
    -- rookies, callups -- which would otherwise be missing from every decision.
    """
    from draftgpt.ingestion.registry_sync import sync_players_from_league_provider
    from draftgpt.providers.registry import build

    settings = get_settings()
    season = season or settings.season
    adapter = build(provider or settings.league_provider)

    with session_scope() as session:
        report = sync_players_from_league_provider(session, adapter, season, create_missing)
    console.print(f"[green]{report.summary()}[/green]")
    if report.unresolved:
        console.print(f"\n[yellow]{len(report.unresolved)} need review:[/yellow]")
        for name in report.unresolved[:20]:
            console.print(f"  {name}")


@sync_app.command("league")
def sync_league_cmd(
    provider: Annotated[str | None, typer.Option(help="Adapter key.")] = None,
    season: Annotated[int | None, typer.Option()] = None,
    owner_team: Annotated[str | None, typer.Option(help="Your team's external id.")] = None,
) -> None:
    """Sync league settings, rules, teams, and rosters."""
    from draftgpt.ingestion.league_sync import sync_league
    from draftgpt.providers.registry import build

    settings = get_settings()
    season = season or settings.season
    adapter = build(provider or settings.league_provider)

    with session_scope() as session:
        report = sync_league(session, adapter, season, owner_team_external_id=owner_team)
    console.print(f"[green]{report.summary()}[/green]")
    for warning in report.warnings:
        console.print(f"  [yellow]{warning}[/yellow]")
    if report.unresolved_players:
        console.print(
            f"\n[red]{len(report.unresolved_players)} player(s) could not be resolved "
            "to a canonical identity:[/red]"
        )
        for name in report.unresolved_players[:20]:
            console.print(f"  {name}")


@sync_app.command("projections")
def sync_projections_cmd(
    week: Annotated[int | None, typer.Option()] = None,
    provider: Annotated[str, typer.Option()] = "fixture_projections",
    season: Annotated[int | None, typer.Option()] = None,
) -> None:
    """Ingest projections for a week."""
    from draftgpt.evaluation.scoring import ScoringRules
    from draftgpt.ingestion.projections_sync import sync_projections
    from draftgpt.providers.registry import build

    settings = get_settings()
    season = season or settings.season
    adapter = build(provider)

    with session_scope() as session:
        rules = _latest_rules(session)
        scoring = (
            ScoringRules.from_config(rules.scoring, f"{rules.id}:v{rules.version}")
            if rules
            else None
        )
        report = sync_projections(
            session, adapter, season, week=week, scope="week" if week else "season", rules=scoring
        )
    console.print(f"[green]{report.summary()}[/green]")
    if report.unresolved:
        console.print(f"[yellow]unresolved: {', '.join(report.unresolved[:10])}[/yellow]")


@sync_app.command("adp")
def sync_adp_cmd(
    provider: Annotated[str | None, typer.Option()] = None,
    season: Annotated[int | None, typer.Option()] = None,
) -> None:
    """Ingest average draft position, which drives availability estimates."""
    from draftgpt.ingestion.projections_sync import sync_adp
    from draftgpt.providers.registry import build

    settings = get_settings()
    season = season or settings.season
    adapter = build(provider or settings.league_provider)

    with session_scope() as session:
        report = sync_adp(session, adapter, season)
    console.print(f"[green]{report.written} ADP rows written[/green]")
    for note in report.notes:
        console.print(f"  [dim]{note}[/dim]")


@sync_app.command("context")
def sync_context(
    season: Annotated[int | None, typer.Option()] = None,
    week: Annotated[int | None, typer.Option()] = None,
) -> None:
    """Ingest schedule, injuries, depth charts, and game context from nflverse."""
    from draftgpt.ingestion.context_sync import sync_context as run_context

    settings = get_settings()
    with session_scope() as session:
        report = run_context(session, season or settings.season, week)
    for line in report.lines():
        console.print(line)


# --------------------------------------------------------------------------
# league
# --------------------------------------------------------------------------
@league_app.command("list")
def league_list() -> None:
    """Show synced leagues and their teams."""
    with session_scope() as session:
        seasons = session.query(LeagueSeason).all()
        if not seasons:
            console.print("[yellow]No leagues synced yet.[/yellow]")
            return
        for league_season in seasons:
            league = session.get(League, league_season.league_id)
            console.print(
                f"\n[bold]{league.name}[/bold] ({league.provider_key}) "
                f"season {league_season.season} -- {league_season.status}"
            )
            table = Table(show_header=True)
            table.add_column("Team id")
            table.add_column("Name")
            table.add_column("Owner?")
            for team_season in session.query(TeamSeason).filter_by(
                league_season_id=league_season.id
            ):
                team = session.get(Team, team_season.team_id)
                table.add_row(
                    team.external_id, team.name, "[green]yes[/green]" if team.is_owner_team else ""
                )
            console.print(table)


@league_app.command("set-owner")
def league_set_owner(team_external_id: str) -> None:
    """Mark which team this system advises."""
    with session_scope() as session:
        teams = session.query(Team).all()
        matched = None
        for team in teams:
            team.is_owner_team = team.external_id == team_external_id
            if team.is_owner_team:
                matched = team
        if matched is None:
            console.print(f"[red]No team with external id '{team_external_id}'.[/red]")
            raise typer.Exit(1)
        console.print(f"[green]Owner team set to {matched.name} ({team_external_id}).[/green]")


# --------------------------------------------------------------------------
# roster
# --------------------------------------------------------------------------
@roster_app.command("week")
def roster_week(
    week: int,
    objective: Annotated[str, typer.Option(help="floor | balanced | ceiling")] = "balanced",
    output_json: Annotated[bool, typer.Option("--json", help="Emit the raw contract.")] = False,
) -> None:
    """Recommend a starting lineup for a week."""
    from draftgpt.commands.roster import recommend_lineup

    with session_scope() as session:
        league_season = session.query(LeagueSeason).first()
        if league_season is None:
            console.print("[red]No league synced. Run `draftgpt sync league`.[/red]")
            raise typer.Exit(1)
        response = recommend_lineup(session, league_season.id, week, objective=objective)
        payload = json.loads(response.model_dump_json())

    if output_json:
        console.print_json(data=payload)
    else:
        render_roster(response)


@roster_app.command("now")
def roster_now(
    objective: Annotated[str, typer.Option()] = "balanced",
    output_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Recommend a lineup for the league's current week."""
    with session_scope() as session:
        league_season = session.query(LeagueSeason).first()
        if league_season is None:
            console.print("[red]No league synced.[/red]")
            raise typer.Exit(1)
        week = league_season.current_week or 1
    roster_week(week=week, objective=objective, output_json=output_json)


def render_roster(response: CommandResponse) -> None:
    """Render the structured contract. The JSON is primary; this derives from it."""
    rec = response.recommendation
    console.print(
        f"\n[bold]Recommended lineup -- week {response.week}[/bold] "
        f"([cyan]{rec.get('objective')}[/cyan])"
    )

    table = Table(show_header=True, header_style="bold")
    table.add_column("Slot", style="cyan", no_wrap=True)
    table.add_column("Player")
    table.add_column("Pos")
    table.add_column("Tm")
    table.add_column("Proj", justify="right")
    table.add_column("Floor", justify="right")
    table.add_column("Ceil", justify="right")
    table.add_column("")
    for starter in rec.get("starters", []):
        flag = ""
        if starter.get("status") in {"Q", "D", "DNP", "LP"}:
            flag = f"[yellow]{starter['status']}[/yellow]"
        table.add_row(
            starter["slot"],
            starter["player"],
            starter["position"],
            starter.get("team") or "",
            f"{starter['projected_points']:.1f}",
            f"{starter['floor']:.1f}",
            f"{starter['ceiling']:.1f}",
            flag,
        )
    console.print(table)
    console.print(
        f"Projected [bold]{rec.get('projected_points')}[/bold] "
        f"(floor {rec.get('floor')}, ceiling {rec.get('ceiling')})  "
        f"confidence [bold]{response.confidence}[/bold]"
    )

    changes = rec.get("changes_from_current") or []
    if changes:
        console.print("\n[bold]Changes from your current lineup[/bold]")
        for change in changes:
            console.print(f"  - {change['instruction']}")
    else:
        console.print("\n[green]Your current lineup is already optimal.[/green]")

    if response.reasons:
        console.print("\n[bold]Why[/bold]")
        for reason in response.reasons:
            console.print(f"  - {reason}")

    if response.conditions:
        console.print("\n[bold]Conditional triggers[/bold]")
        for condition in response.conditions:
            console.print(f"  - if {condition.trigger}: {condition.action}")

    if response.watch:
        console.print("\n[bold]Watch[/bold]")
        for item in response.watch:
            console.print(f"  - {item.action}" + (f" ({item.detail})" if item.detail else ""))

    if response.freshness.warnings:
        console.print("\n[yellow]Freshness warnings[/yellow]")
        for warning in response.freshness.warnings:
            console.print(f"  - {warning}")

    check_again = (
        response.check_again_at.isoformat(timespec="minutes")
        if response.check_again_at
        else "n/a"
    )
    console.print(
        f"\n[dim]snapshot {response.snapshot_id[:12]} - calc {response.calculation_version} - "
        f"check again {check_again}[/dim]"
    )


# --------------------------------------------------------------------------
# prep
# --------------------------------------------------------------------------
IMPACT_STYLE = {
    "defining": "bold red",
    "high": "bold yellow",
    "moderate": "cyan",
    "minor": "dim",
}


@prep_app.command("league")
def prep_league_cmd(
    output_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """What your league settings imply for strategy."""
    from draftgpt.commands.prep import prep_league

    with session_scope() as session:
        response = prep_league(session)
        payload = json.loads(response.model_dump_json())

    if output_json:
        console.print_json(data=payload)
        return

    league = response.recommendation["league"]
    console.print(
        f"\n[bold]{league['scoring_format']} - {league['team_count']} teams - "
        f"{'SUPERFLEX' if league['is_superflex'] else '1QB'}[/bold]"
    )
    console.print(
        f"[dim]Starting: {' / '.join(response.recommendation['starting_lineup'])}   "
        f"Bench: {league['bench_slots']}   FAAB: {league['faab_budget']}[/dim]\n"
    )

    demand = Table(title="Positional demand", show_header=True)
    demand.add_column("Pos", style="cyan")
    demand.add_column("Starters league-wide", justify="right")
    demand.add_column("Per team", justify="right")
    demand.add_column("Replacement rank", justify="right")
    for row in response.recommendation["positional_demand"]:
        demand.add_row(
            row["position"],
            f"{row['starters_league_wide']:.0f}",
            f"{row['per_team']:.2f}",
            f"{row['position']}{row['replacement_rank']}",
        )
    console.print(demand)

    console.print("\n[bold]What this means[/bold]")
    for finding in response.recommendation["findings"]:
        style = IMPACT_STYLE.get(finding["impact"], "")
        console.print(f"\n[{style}]{finding['impact'].upper()}[/{style}]  {finding['headline']}")
        console.print(f"  {finding['detail']}")

    console.print(f"\n[dim]calc {response.calculation_version}[/dim]")


@prep_app.command("draft")
def prep_draft_cmd(
    position: Annotated[str | None, typer.Option("--position", "-p")] = None,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 30,
    at_pick: Annotated[int | None, typer.Option(help="Estimate availability at this pick.")] = None,
    draft_position: Annotated[int | None, typer.Option(help="Your draft slot.")] = None,
    tier_max: Annotated[int | None, typer.Option(help="Only show tiers up to this.")] = None,
    output_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """The draft board, priced under your league's rules."""
    from draftgpt.commands.prep import prep_draft

    with session_scope() as session:
        response = prep_draft(
            session,
            position=position,
            limit=limit,
            at_pick=at_pick,
            draft_position=draft_position,
            tier_max=tier_max,
        )
        payload = json.loads(response.model_dump_json())

    if output_json:
        console.print_json(data=payload)
        return

    rec = response.recommendation
    if not rec["board"]:
        console.print("[yellow]Board is empty.[/yellow]")
        for warning in rec.get("warnings", []):
            console.print(f"  [yellow]{warning}[/yellow]")
        return

    table = Table(title=f"Draft board ({rec['filtered_to']} of {rec['total_ranked']})")
    table.add_column("#", justify="right", style="dim")
    table.add_column("Player")
    table.add_column("Pos", style="cyan")
    table.add_column("Tm")
    table.add_column("Proj", justify="right")
    table.add_column("VOR", justify="right")
    table.add_column("Tier", justify="right")
    table.add_column("ADP", justify="right")
    table.add_column("Bye", justify="right")

    for row in rec["board"]:
        cliff = " [red]<- tier ends[/red]" if row["is_last_of_tier"] else ""
        table.add_row(
            str(row["overall_rank"]),
            row["player"] + cliff,
            row["position_rank"],
            row["team"] or "",
            f"{row['projected_points']:.0f}",
            f"{row['value_over_replacement']:+.0f}",
            str(row["tier"]),
            f"{row['adp']:.0f}" if row["adp"] else "-",
            str(row["bye_week"] or "-"),
        )
    console.print(table)

    scarcity = Table(title="Scarcity")
    scarcity.add_column("Pos", style="cyan")
    scarcity.add_column("Needed", justify="right")
    scarcity.add_column("Above replacement", justify="right")
    scarcity.add_column("Surplus", justify="right")
    for row in rec["scarcity"]:
        surplus = row["surplus"]
        style = "red" if surplus <= 0 else "green"
        scarcity.add_row(
            row["position"],
            f"{row['starters_needed_league_wide']:.0f}",
            str(row["available_above_replacement"]),
            f"[{style}]{surplus:+d}[/{style}]",
        )
    console.print(scarcity)

    if rec.get("availability_at_pick"):
        console.print(f"\n[bold]Chance still available at pick {at_pick}[/bold]")
        for row in rec["availability_at_pick"][:12]:
            console.print(
                f"  {row['player']:<28} {row['survival_probability']:.0%}  "
                f"[dim](ADP {row['adp']:.0f})[/dim]"
            )

    for warning in rec.get("warnings", []):
        console.print(f"\n[yellow]{warning}[/yellow]")


@prep_app.command("player")
def prep_player_cmd(
    name: str,
    at_pick: Annotated[int | None, typer.Option()] = None,
    output_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Value, tier, and availability for one player."""
    from draftgpt.commands.prep import prep_player
    from draftgpt.ingestion.identity import AmbiguousPlayerError

    try:
        with session_scope() as session:
            response = prep_player(session, name, at_pick=at_pick)
            payload = json.loads(response.model_dump_json())
    except AmbiguousPlayerError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    except LookupError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    if output_json:
        console.print_json(data=payload)
        return

    rec = response.recommendation
    console.print(f"\n[bold]{rec.get('player', name)}[/bold]")
    if rec.get("note"):
        console.print(f"[yellow]{rec['note']}[/yellow]")
        return
    console.print(
        f"{rec['position_rank']} - tier {rec['tier']} - overall value rank "
        f"{rec['overall_rank']}"
    )
    console.print("\n[bold]Why[/bold]")
    for reason in response.reasons:
        console.print(f"  - {reason}")

    if rec.get("positional_neighbours"):
        table = Table(title="Nearby at this position")
        table.add_column("Player")
        table.add_column("Proj", justify="right")
        table.add_column("VOR", justify="right")
        table.add_column("Tier", justify="right")
        for row in rec["positional_neighbours"]:
            marker = " [cyan]<-[/cyan]" if row["player_id"] == rec["player_id"] else ""
            table.add_row(
                row["player"] + marker,
                f"{row['projected_points']:.0f}",
                f"{row['value_over_replacement']:+.0f}",
                str(row["tier"]),
            )
        console.print(table)


@app.command("mcp")
def mcp_serve() -> None:
    """Run the MCP server over stdio so a chat client can call these tools."""
    from draftgpt.interfaces.mcp_server import main as mcp_main

    mcp_main()


# --------------------------------------------------------------------------
# demo
# --------------------------------------------------------------------------
@app.command("demo")
def demo(
    week: Annotated[int, typer.Option()] = 1,
    objective: Annotated[str, typer.Option()] = "balanced",
) -> None:
    """Seed the bundled fixture league and run `/roster` end to end.

    Requires no credentials and no network -- this is the reproducible path for
    verifying the whole pipeline.
    """
    from draftgpt.commands.roster import recommend_lineup
    from draftgpt.database.seed import BYE_TEAMS, SEASON, build_fixture_files
    from draftgpt.evaluation.scoring import ScoringRules
    from draftgpt.ingestion.league_sync import sync_league
    from draftgpt.ingestion.projections_sync import sync_adp, sync_projections
    from draftgpt.ingestion.registry_sync import (
        ensure_nfl_teams,
        sync_players_from_league_provider,
    )
    from draftgpt.ingestion.runner import sync_provider_registry
    from draftgpt.providers.registry import all_specs, build

    settings = get_settings()
    paths = build_fixture_files(Path(settings.fixtures_dir))
    console.print(f"[dim]fixtures written to {paths['league_settings'].parent}[/dim]")

    with session_scope() as session:
        sync_provider_registry(session, all_specs())
        teams = ensure_nfl_teams(session)
        for abbreviation, bye_week in BYE_TEAMS.items():
            team = teams.get(abbreviation)
            if team is not None:
                team.bye_week_by_season = {str(SEASON): bye_week}

        # Canonical identities must exist before rosters can reference them.
        registry_report = sync_players_from_league_provider(
            session, build("fixture_league"), SEASON
        )
        console.print(f"[green]players:[/green] {registry_report.summary()}")

        league_report = sync_league(
            session, build("fixture_league"), SEASON, owner_team_external_id="1"
        )
        console.print(f"[green]league:[/green] {league_report.summary()}")

        rules = _latest_rules(session)
        scoring = ScoringRules.from_config(rules.scoring, f"{rules.id}:v{rules.version}")
        projections = build("fixture_projections")
        weekly = sync_projections(
            session, projections, SEASON, week=week, scope="week", rules=scoring
        )
        console.print(f"[green]weekly projections:[/green] {weekly.summary()}")

        season_report = sync_projections(
            session, projections, SEASON, scope="season", rules=scoring
        )
        console.print(f"[green]season projections:[/green] {season_report.summary()}")

        adp_report = sync_adp(session, projections, SEASON)
        console.print(f"[green]adp:[/green] {adp_report.written} rows")

        response = recommend_lineup(session, league_report.league_season_id, week, objective)

    render_roster(response)
    console.print(
        "\n[dim]Also try: draftgpt prep league   |   draftgpt prep draft   |   "
        "draftgpt prep player <name>[/dim]"
    )


@app.command("db-upgrade")
def db_upgrade() -> None:
    """Apply migrations to the configured database."""
    from alembic import command
    from alembic.config import Config

    get_engine()
    config = Config("alembic.ini")
    command.upgrade(config, "head")
    console.print("[green]Migrations applied.[/green]")


def _latest_rules(session):
    from draftgpt.database.models import LeagueRules

    return (
        session.query(LeagueRules).order_by(LeagueRules.version.desc()).first()
    )


if __name__ == "__main__":
    app()
