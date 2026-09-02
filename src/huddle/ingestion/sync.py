"""Pulling league data into the database.

Every fetch runs inside a tracked ingestion run, so freshness is always
answerable: the show can say "scores are from an hour ago" because the record
of that fetch exists, not because someone assumed it.

Failures are non-fatal by design. A league whose feed is down loses its
segment, not the whole show -- a child who hears five segments instead of six
has still had their morning.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from huddle.catalog import LEAGUES, LeagueSpec, get_league
from huddle.database.models import Athlete, Game, NewsStory, Team
from huddle.domain.safety import SafetyFilter
from huddle.ingestion.runner import fetch_with_run, upsert_provider
from huddle.providers.base import Provider
from huddle.providers.registry import build

logger = logging.getLogger(__name__)


@dataclass
class SyncReport:
    teams: int = 0
    games: int = 0
    athletes: int = 0
    stories_seen: int = 0
    stories_allowed: int = 0
    skipped: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        out = [
            f"teams: {self.teams}",
            f"games: {self.games}",
            f"athletes: {self.athletes}",
            f"stories: {self.stories_allowed} allowed of {self.stories_seen} seen",
        ]
        out += [f"skipped {item}" for item in self.skipped]
        return out


def _provider(provider: Provider | None) -> Provider:
    return provider or build("espn")


# -- teams -----------------------------------------------------------------
def sync_teams(
    session: Session,
    league_keys: list[str] | None = None,
    provider: Provider | None = None,
) -> SyncReport:
    """Refresh the team list for each league. Safe to run repeatedly."""
    provider = _provider(provider)
    db_provider = upsert_provider(session, provider.spec)
    report = SyncReport()

    for spec in _specs(league_keys):
        result = fetch_with_run(
            session, provider, "league_teams",
            scope=f"league:{spec.key}", required=False, league=spec.key,
        )
        if result is None:
            report.skipped.append(f"teams:{spec.key}")
            continue

        existing = _teams_by_external_id(session, spec.key)
        now = datetime.now(UTC)
        for record in result.records:
            team = existing.get(record["external_id"])
            if team is None:
                team = Team(league_key=spec.key, external_id=record["external_id"])
                session.add(team)
                existing[record["external_id"]] = team
            _apply_team(team, record, db_provider.id, now)
            report.teams += 1
        session.flush()

    return report


def _apply_team(team: Team, record: dict, provider_id: str, now: datetime) -> None:
    team.display_name = record["display_name"]
    team.short_name = record["short_name"]
    team.location = record.get("location")
    team.abbreviation = record.get("abbreviation")
    team.search_name = record["search_name"]
    team.slug = record.get("slug")
    team.color = record.get("color")
    team.logo_url = record.get("logo_url")
    team.active = record.get("active", True)
    if record.get("record_summary"):
        team.record_summary = record["record_summary"]
    if record.get("standing_summary"):
        team.standing_summary = record["standing_summary"]
    team.provider_id = provider_id
    team.retrieved_at = now


def _teams_by_external_id(session: Session, league_key: str) -> dict[str, Team]:
    return {
        team.external_id: team
        for team in session.scalars(select(Team).where(Team.league_key == league_key))
    }


# -- games -----------------------------------------------------------------
def sync_scoreboard(
    session: Session,
    league_keys: list[str] | None = None,
    on: date | None = None,
    provider: Provider | None = None,
) -> SyncReport:
    """Today's scores and schedule for each league."""
    provider = _provider(provider)
    db_provider = upsert_provider(session, provider.spec)
    report = SyncReport()

    for spec in _specs(league_keys):
        result = fetch_with_run(
            session, provider, "scoreboard",
            scope=f"league:{spec.key}", required=False, league=spec.key, on=on,
        )
        if result is None:
            report.skipped.append(f"scoreboard:{spec.key}")
            continue
        report.games += _write_games(session, spec, result.records, db_provider.id)

    return report


def sync_team_details(
    session: Session,
    team_ids: list[str],
    provider: Provider | None = None,
) -> SyncReport:
    """Record, standing, and next-or-last game for specific followed teams.

    Called with only the teams somebody actually follows, which keeps a
    fourteen-league catalogue affordable to refresh every morning.
    """
    provider = _provider(provider)
    db_provider = upsert_provider(session, provider.spec)
    report = SyncReport()
    if not team_ids:
        return report

    teams = list(session.scalars(select(Team).where(Team.id.in_(team_ids))))
    now = datetime.now(UTC)

    for team in teams:
        spec = get_league(team.league_key)
        result = fetch_with_run(
            session, provider, "team_detail",
            scope=f"team:{team.league_key}:{team.external_id}", required=False,
            league=team.league_key, team_external_id=team.external_id,
        )
        if result is None or not result.records:
            report.skipped.append(f"team_detail:{team.league_key}:{team.external_id}")
            continue

        record = result.records[0]
        _apply_team(team, record, db_provider.id, now)
        report.teams += 1
        report.games += _write_games(session, spec, record.get("games") or [], db_provider.id)

    return report


def _write_games(
    session: Session, spec: LeagueSpec, records: list[dict], provider_id: str
) -> int:
    if not records:
        return 0
    teams = _teams_by_external_id(session, spec.key)
    existing = {
        game.external_id: game
        for game in session.scalars(
            select(Game).where(
                Game.league_key == spec.key,
                Game.external_id.in_([r["external_id"] for r in records]),
            )
        )
    }
    now = datetime.now(UTC)
    written = 0

    for record in records:
        game = existing.get(record["external_id"])
        if game is None:
            game = Game(league_key=spec.key, external_id=record["external_id"])
            session.add(game)
            existing[record["external_id"]] = game

        game.name = record.get("name")
        game.start_at = record.get("start_at")
        game.state = record.get("state") or "pre"
        game.status_detail = record.get("status_detail")
        game.completed = bool(record.get("completed"))
        game.home_score = record.get("home_score")
        game.away_score = record.get("away_score")
        game.venue = record.get("venue")
        game.broadcast = record.get("broadcast")
        game.extra = record.get("extra") or {}
        # A scoreboard can name a team we have not ingested yet (a cup tie
        # against a lower division, say). The game is still worth keeping;
        # the writer falls back to the name carried on the record.
        home = teams.get(record.get("home_team_external_id") or "")
        away = teams.get(record.get("away_team_external_id") or "")
        game.home_team_id = home.id if home else None
        game.away_team_id = away.id if away else None
        game.extra = {
            **game.extra,
            "home_team_name": record.get("home_team_name"),
            "away_team_name": record.get("away_team_name"),
        }
        game.provider_id = provider_id
        game.retrieved_at = now
        written += 1

    session.flush()
    return written


# -- athletes --------------------------------------------------------------
def sync_rosters(
    session: Session,
    team_ids: list[str],
    provider: Provider | None = None,
) -> SyncReport:
    """Rosters for followed teams, which is where Birthday Club gets its names."""
    provider = _provider(provider)
    db_provider = upsert_provider(session, provider.spec)
    report = SyncReport()

    teams = list(session.scalars(select(Team).where(Team.id.in_(team_ids or []))))
    now = datetime.now(UTC)

    for team in teams:
        result = fetch_with_run(
            session, provider, "team_roster",
            scope=f"roster:{team.league_key}:{team.external_id}", required=False,
            league=team.league_key, team_external_id=team.external_id,
        )
        if result is None:
            report.skipped.append(f"roster:{team.league_key}:{team.external_id}")
            continue

        existing = {
            athlete.external_id: athlete
            for athlete in session.scalars(
                select(Athlete).where(
                    Athlete.league_key == team.league_key, Athlete.team_id == team.id
                )
            )
        }
        for record in result.records:
            athlete = existing.get(record["external_id"])
            if athlete is None:
                athlete = Athlete(
                    league_key=team.league_key, external_id=record["external_id"]
                )
                session.add(athlete)
            athlete.team_id = team.id
            athlete.full_name = record["full_name"]
            athlete.position = record.get("position")
            athlete.jersey = record.get("jersey")
            athlete.birth_date = record.get("birth_date")
            athlete.birth_month = record.get("birth_month")
            athlete.birth_day = record.get("birth_day")
            athlete.birth_place = record.get("birth_place")
            athlete.headshot_url = record.get("headshot_url")
            athlete.active = record.get("active", True)
            athlete.provider_id = db_provider.id
            athlete.retrieved_at = now
            report.athletes += 1
        session.flush()

    return report


# -- news ------------------------------------------------------------------
def sync_news(
    session: Session,
    league_keys: list[str] | None = None,
    provider: Provider | None = None,
    safety: SafetyFilter | None = None,
    limit: int = 30,
) -> SyncReport:
    """Fetch candidate stories and record the safety verdict for each.

    Blocked stories are stored too. Keeping them is what makes the parent
    dashboard's "here is what we filtered out" honest rather than decorative.
    """
    provider = _provider(provider)
    db_provider = upsert_provider(session, provider.spec)
    safety = safety or SafetyFilter()
    report = SyncReport()

    for spec in _specs(league_keys):
        result = fetch_with_run(
            session, provider, "league_news",
            scope=f"league:{spec.key}", required=False, league=spec.key, limit=limit,
        )
        if result is None:
            report.skipped.append(f"news:{spec.key}")
            continue

        existing = {
            story.external_id: story
            for story in session.scalars(
                select(NewsStory).where(
                    NewsStory.league_key == spec.key,
                    NewsStory.external_id.in_([r["external_id"] for r in result.records]),
                )
            )
        }
        now = datetime.now(UTC)
        for record in result.records:
            verdict = safety.check_story(record)
            story = existing.get(record["external_id"])
            if story is None:
                story = NewsStory(
                    league_key=spec.key, external_id=record["external_id"]
                )
                session.add(story)
                existing[record["external_id"]] = story

            story.headline = record["headline"]
            story.summary = record.get("summary")
            story.story_type = record.get("type")
            story.url = record.get("url")
            story.published_at = record.get("published_at")
            story.categories = record.get("categories") or []
            story.team_external_ids = record.get("team_external_ids") or []
            story.safety_verdict = str(verdict.verdict)
            story.safety_category = verdict.category
            story.safety_matched = verdict.matched
            story.caution = verdict.caution
            story.provider_id = db_provider.id
            story.retrieved_at = now

            report.stories_seen += 1
            if verdict.allowed:
                report.stories_allowed += 1
        session.flush()

    return report


def _specs(league_keys: list[str] | None) -> list[LeagueSpec]:
    if not league_keys:
        return list(LEAGUES)
    return [get_league(key) for key in league_keys]


def followed_league_keys(session: Session) -> list[str]:
    """Every league at least one family follows. Nothing else is worth syncing."""
    from huddle.database.models import FavoriteTeam

    rows = session.execute(
        select(Team.league_key).join(FavoriteTeam, FavoriteTeam.team_id == Team.id).distinct()
    )
    return sorted({row[0] for row in rows})
