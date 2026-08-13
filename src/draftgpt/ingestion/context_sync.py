"""Context ingestion from nflverse: schedule, games, injuries, depth charts.

Each capability is fetched independently and failures are non-fatal. A missing
depth chart should reduce confidence in a recommendation, never prevent one
(brief section 4).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from draftgpt.database.models import (
    DepthChartSnapshot,
    MarketContextSnapshot,
    NflGame,
    NflTeam,
    PlayerProviderId,
    PlayerStatusEvent,
    WeatherSnapshot,
)
from draftgpt.ingestion.registry_sync import ensure_nfl_teams
from draftgpt.ingestion.runner import fetch_with_run, upsert_provider
from draftgpt.providers.base import Provider
from draftgpt.providers.registry import build

logger = logging.getLogger(__name__)


@dataclass
class ContextSyncReport:
    games: int = 0
    weather: int = 0
    betting: int = 0
    injuries: int = 0
    depth_chart_rows: int = 0
    byes_set: int = 0
    skipped: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        out = [
            f"[green]schedule:[/green] {self.games} games, {self.byes_set} bye weeks derived",
            f"[green]context:[/green] {self.weather} weather, {self.betting} betting rows",
            f"[green]status:[/green] {self.injuries} injury/practice events",
            f"[green]depth charts:[/green] {self.depth_chart_rows} rows",
        ]
        out += [f"[yellow]skipped {s}[/yellow]" for s in self.skipped]
        return out


def sync_context(
    session: Session,
    season: int,
    week: int | None = None,
    provider: Provider | None = None,
) -> ContextSyncReport:
    provider = provider or build("nflverse")
    db_provider = upsert_provider(session, provider.spec)
    report = ContextSyncReport()
    teams = ensure_nfl_teams(session)
    scope = f"season:{season}"

    game_ids = _sync_schedule(session, provider, db_provider.id, season, teams, report, scope)
    _sync_game_context(session, provider, db_provider.id, season, game_ids, report, scope)
    _sync_injuries(session, provider, db_provider.id, season, week, report, scope)
    _sync_depth_charts(session, provider, db_provider.id, season, week, teams, report, scope)

    session.flush()
    return report


def _sync_schedule(
    session, provider, provider_id, season, teams, report, scope
) -> dict[str, str]:
    result = fetch_with_run(
        session, provider, "schedule", scope=scope, required=False, season=season
    )
    if result is None:
        report.skipped.append("schedule")
        return {}

    existing = {
        (g.season, g.week, g.home_team_id): g
        for g in session.scalars(select(NflGame).where(NflGame.season == season))
    }
    external_to_id: dict[str, str] = {}
    played_weeks: dict[str, set[int]] = {}

    for record in result.records:
        home = teams.get(record.get("home_team") or "")
        away = teams.get(record.get("away_team") or "")
        week = record.get("week")
        if not (home and away and week):
            continue

        key = (season, week, home.id)
        game = existing.get(key)
        if game is None:
            game = NflGame(
                season=season, week=week, home_team_id=home.id, away_team_id=away.id
            )
            session.add(game)
            existing[key] = game
        game.away_team_id = away.id
        game.kickoff_at = record.get("kickoff_at")
        game.venue = record.get("stadium")
        game.is_dome = record.get("is_dome")
        game.home_score = record.get("home_score")
        game.away_score = record.get("away_score")
        game.status = "final" if record.get("home_score") is not None else "scheduled"
        game.provider_id = provider_id
        game.retrieved_at = datetime.now(UTC)
        session.flush()
        report.games += 1

        if record.get("external_id"):
            external_to_id[record["external_id"]] = game.id
        # Only regular-season games define a bye.
        if record.get("game_type") in (None, "REG"):
            for abbreviation in (record.get("home_team"), record.get("away_team")):
                if abbreviation:
                    played_weeks.setdefault(abbreviation, set()).add(int(week))

    report.byes_set = _derive_byes(teams, played_weeks, season)
    session.flush()
    return external_to_id


def _derive_byes(
    teams: dict[str, NflTeam], played_weeks: dict[str, set[int]], season: int
) -> int:
    """A team's bye is the regular-season week it does not appear in.

    Derived from the schedule rather than hardcoded, so it stays correct as the
    league changes its regular-season length.
    """
    if not played_weeks:
        return 0
    max_week = max(week for weeks in played_weeks.values() for week in weeks)
    count = 0
    for abbreviation, weeks in played_weeks.items():
        team = teams.get(abbreviation)
        if team is None:
            continue
        missing = sorted(set(range(1, max_week + 1)) - weeks)
        if len(missing) == 1:
            existing = dict(team.bye_week_by_season or {})
            existing[str(season)] = missing[0]
            team.bye_week_by_season = existing
            count += 1
    return count


def _sync_game_context(
    session, provider, provider_id, season, game_ids, report, scope
) -> None:
    if not game_ids:
        return

    betting = fetch_with_run(
        session, provider, "betting_context", scope=scope, required=False, season=season
    )
    if betting is None:
        report.skipped.append("betting_context")
    else:
        for record in betting.records:
            game_id = game_ids.get(record.get("game_external_id") or "")
            if not game_id:
                continue
            session.add(
                MarketContextSnapshot(
                    nfl_game_id=game_id,
                    total=record.get("total"),
                    home_spread=record.get("home_spread"),
                    home_implied_total=record.get("home_implied_total"),
                    away_implied_total=record.get("away_implied_total"),
                    provider_id=provider_id,
                    retrieved_at=datetime.now(UTC),
                )
            )
            report.betting += 1

    weather = fetch_with_run(
        session, provider, "weather", scope=scope, required=False, season=season
    )
    if weather is None:
        report.skipped.append("weather")
        return
    for record in weather.records:
        game_id = game_ids.get(record.get("game_external_id") or "")
        if not game_id:
            continue
        session.add(
            WeatherSnapshot(
                nfl_game_id=game_id,
                temperature_f=record.get("temperature_f"),
                wind_mph=record.get("wind_mph"),
                conditions=record.get("conditions"),
                provider_id=provider_id,
                retrieved_at=datetime.now(UTC),
            )
        )
        report.weather += 1


def _gsis_map(session: Session) -> dict[str, str]:
    return {
        m.external_id: m.player_id
        for m in session.scalars(
            select(PlayerProviderId).where(PlayerProviderId.provider_key == "nflverse")
        )
    }


def _sync_injuries(session, provider, provider_id, season, week, report, scope) -> None:
    result = fetch_with_run(
        session, provider, "injuries", scope=scope, required=False, season=season, week=week
    )
    if result is None:
        report.skipped.append("injuries")
        return

    by_gsis = _gsis_map(session)
    now = datetime.now(UTC)
    for record in result.records:
        player_id = by_gsis.get(str(record.get("player_external_id")))
        if not player_id or not record.get("designation"):
            continue
        session.add(
            PlayerStatusEvent(
                player_id=player_id,
                season=record.get("season") or season,
                week=record.get("week"),
                event_type=record.get("event_type") or "injury",
                designation=record.get("designation"),
                body_part=record.get("body_part"),
                detail=record.get("detail"),
                provider_id=provider_id,
                retrieved_at=now,
                effective_at=now,
            )
        )
        report.injuries += 1


def _sync_depth_charts(
    session, provider, provider_id, season, week, teams, report, scope
) -> None:
    result = fetch_with_run(
        session, provider, "depth_charts", scope=scope, required=False, season=season, week=week
    )
    if result is None:
        report.skipped.append("depth_charts")
        return

    by_gsis = _gsis_map(session)
    now = datetime.now(UTC)
    for record in result.records:
        player_id = by_gsis.get(str(record.get("player_external_id")))
        team = teams.get(record.get("team") or "")
        if not player_id or team is None:
            continue
        session.add(
            DepthChartSnapshot(
                nfl_team_id=team.id,
                season=record.get("season") or season,
                week=record.get("week"),
                position=record.get("position") or "UNK",
                player_id=player_id,
                rank=record.get("rank") or 99,
                role=record.get("role"),
                provider_id=provider_id,
                retrieved_at=now,
            )
        )
        report.depth_chart_rows += 1
