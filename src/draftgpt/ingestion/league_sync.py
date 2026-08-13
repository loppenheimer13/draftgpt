"""League state sync: settings, rules, roster slots, teams, and rosters.

Works against any adapter declaring the league capabilities, so the ESPN
adapter and the fixture adapter follow identical code paths -- which is what
makes the fixture golden scenarios meaningful tests of production behavior.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from draftgpt.database.models import (
    League,
    LeagueRules,
    LeagueSeason,
    Player,
    PlayerProviderId,
    PlayerStatusEvent,
    RosterMembership,
    RosterSlot,
    Team,
    TeamSeason,
)
from draftgpt.domain.enums import SLOT_ELIGIBILITY, SlotType
from draftgpt.ingestion.registry_sync import link_league_players
from draftgpt.ingestion.runner import fetch_with_run, upsert_provider
from draftgpt.providers.base import Provider

logger = logging.getLogger(__name__)


@dataclass
class LeagueSyncReport:
    league_id: str | None = None
    league_season_id: str | None = None
    teams_synced: int = 0
    roster_entries: int = 0
    players_linked: int = 0
    status_events: int = 0
    unresolved_players: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        base = (
            f"{self.teams_synced} teams, {self.roster_entries} roster entries, "
            f"{self.players_linked} players linked, {self.status_events} status events"
        )
        if self.unresolved_players:
            base += f", {len(self.unresolved_players)} UNRESOLVED"
        return base


def sync_league(
    session: Session,
    provider: Provider,
    season: int,
    owner_team_external_id: str | None = None,
) -> LeagueSyncReport:
    report = LeagueSyncReport()
    upsert_provider(session, provider.spec)
    provider_key = provider.spec.key

    settings_result = fetch_with_run(
        session, provider, "league_settings", scope=f"season:{season}"
    )
    if settings_result is None or not settings_result.records:
        report.warnings.append("league settings unavailable; nothing synced")
        return report
    report.warnings.extend(settings_result.notes)
    settings = settings_result.records[0]

    league = _upsert_league(session, provider_key, settings)
    league_season = _upsert_league_season(session, league, settings, season)
    rules = _upsert_rules(session, league_season, settings)
    _upsert_roster_slots(session, rules, settings.get("roster_slots", []))

    report.league_id = league.id
    report.league_season_id = league_season.id

    rosters_result = fetch_with_run(
        session, provider, "league_rosters", scope=f"season:{season}", required=False
    )
    if rosters_result is None:
        report.warnings.append("rosters unavailable; league configured but rosters not synced")
        session.flush()
        return report

    all_entries = [
        entry for team in rosters_result.records for entry in team.get("players", [])
    ]
    linked, unresolved = link_league_players(session, provider_key, all_entries)
    report.players_linked = linked
    report.unresolved_players = unresolved

    id_map = _provider_id_map(session, provider_key)

    for team_record in rosters_result.records:
        team = _upsert_team(session, league, team_record, owner_team_external_id)
        team_season = _upsert_team_season(session, team, league_season, team_record)
        report.teams_synced += 1
        report.roster_entries += _sync_roster(
            session, league_season, team_season, team_record.get("players", []), id_map
        )

    # League platforms carry their own injury designation on roster entries.
    # It is often the freshest signal available (platforms update it fast), so
    # it is recorded as a status event rather than discarded.
    report.status_events = _sync_roster_status(
        session, league_season, all_entries, id_map, provider.spec.key
    )

    session.flush()
    logger.info("league sync: %s", report.summary())
    return report


# --------------------------------------------------------------------------
def _upsert_league(session: Session, provider_key: str, settings: dict) -> League:
    external_id = str(settings["external_id"])
    league = session.scalar(
        select(League).where(
            League.provider_key == provider_key, League.external_id == external_id
        )
    )
    if league is None:
        league = League(provider_key=provider_key, external_id=external_id)
        session.add(league)
    league.name = settings.get("name") or external_id
    league.format = settings.get("format", "redraft")
    session.flush()
    return league


def _upsert_league_season(
    session: Session, league: League, settings: dict, season: int
) -> LeagueSeason:
    row = session.scalar(
        select(LeagueSeason).where(
            LeagueSeason.league_id == league.id, LeagueSeason.season == season
        )
    )
    if row is None:
        row = LeagueSeason(league_id=league.id, season=season)
        session.add(row)
    row.team_count = int(settings.get("team_count") or 0)
    row.current_week = settings.get("current_week")
    row.regular_season_weeks = int(settings.get("regular_season_weeks") or 14)
    row.playoff_weeks = settings.get("playoff_weeks") or []
    row.playoff_team_count = settings.get("playoff_team_count")
    row.trade_deadline_at = settings.get("trade_deadline_at")
    row.status = settings.get("status", "pre_draft")
    row.retrieved_at = datetime.now(UTC)
    session.flush()
    return row


def _upsert_rules(session: Session, league_season: LeagueSeason, settings: dict) -> LeagueRules:
    """Create a new rules version when scoring changes; never mutate history."""
    scoring = settings.get("scoring") or {}
    latest = session.scalars(
        select(LeagueRules)
        .where(LeagueRules.league_season_id == league_season.id)
        .order_by(LeagueRules.version.desc())
    ).first()

    if latest is not None and latest.scoring == scoring:
        latest.raw_settings = settings.get("raw_settings") or {}
        session.flush()
        return latest

    version = (latest.version + 1) if latest else 1
    rules = LeagueRules(
        league_season_id=league_season.id,
        version=version,
        effective_from=datetime.now(UTC),
        scoring=scoring,
        waiver_type=settings.get("waiver_type", "faab"),
        faab_budget=settings.get("faab_budget"),
        waiver_process_days=settings.get("waiver_process_days") or [],
        roster_size=settings.get("roster_size"),
        ir_slots=int(settings.get("ir_slots") or 0),
        trade_review_type=settings.get("trade_review_type"),
        raw_settings=settings.get("raw_settings") or {},
        retrieved_at=datetime.now(UTC),
    )
    session.add(rules)
    session.flush()
    if latest is not None:
        logger.info("league scoring changed -> rules version %s", version)
    return rules


def _upsert_roster_slots(session: Session, rules: LeagueRules, slot_records: list[dict]) -> None:
    """Expand slot counts into one row per startable position."""
    existing = list(
        session.scalars(select(RosterSlot).where(RosterSlot.league_rules_id == rules.id))
    )
    if existing:
        return

    display = 0
    for record in slot_records:
        slot = str(record["slot"])
        eligible = record.get("eligible_positions") or list(SLOT_ELIGIBILITY.get(slot, ()))
        for ordinal in range(int(record.get("count", 0))):
            session.add(
                RosterSlot(
                    league_rules_id=rules.id,
                    slot=slot,
                    ordinal=ordinal,
                    eligible_positions=eligible,
                    is_starting=slot not in {SlotType.BENCH, SlotType.IR},
                    display_order=display,
                )
            )
            display += 1
    session.flush()


def _upsert_team(
    session: Session, league: League, record: dict, owner_external_id: str | None
) -> Team:
    external_id = str(record["team_external_id"])
    team = session.scalar(
        select(Team).where(Team.league_id == league.id, Team.external_id == external_id)
    )
    if team is None:
        team = Team(league_id=league.id, external_id=external_id)
        session.add(team)
    team.name = record.get("team_name") or f"Team {external_id}"
    if owner_external_id is not None:
        team.is_owner_team = external_id == str(owner_external_id)
    session.flush()
    return team


def _upsert_team_season(
    session: Session, team: Team, league_season: LeagueSeason, record: dict
) -> TeamSeason:
    row = session.scalar(
        select(TeamSeason).where(
            TeamSeason.team_id == team.id,
            TeamSeason.league_season_id == league_season.id,
        )
    )
    if row is None:
        row = TeamSeason(team_id=team.id, league_season_id=league_season.id)
        session.add(row)
    row.wins = int(record.get("wins") or 0)
    row.losses = int(record.get("losses") or 0)
    row.ties = int(record.get("ties") or 0)
    row.points_for = float(record.get("points_for") or 0.0)
    row.points_against = float(record.get("points_against") or 0.0)
    row.faab_remaining = record.get("faab_remaining")
    row.waiver_priority = record.get("waiver_priority")
    row.draft_position = record.get("draft_position")
    row.retrieved_at = datetime.now(UTC)
    session.flush()
    return row


def _provider_id_map(session: Session, provider_key: str) -> dict[str, str]:
    return {
        m.external_id: m.player_id
        for m in session.scalars(
            select(PlayerProviderId).where(PlayerProviderId.provider_key == provider_key)
        )
    }


def _sync_roster(
    session: Session,
    league_season: LeagueSeason,
    team_season: TeamSeason,
    entries: list[dict],
    id_map: dict[str, str],
) -> int:
    """Reconcile roster membership as intervals.

    Players no longer on the roster get their interval closed rather than
    deleted, so any past week's lineup can still be reconstructed exactly.
    """
    now = datetime.now(UTC)
    incoming: dict[str, dict] = {}
    for entry in entries:
        player_id = id_map.get(str(entry.get("player_external_id")))
        if player_id:
            incoming[player_id] = entry

    open_rows = {
        row.player_id: row
        for row in session.scalars(
            select(RosterMembership).where(
                RosterMembership.team_season_id == team_season.id,
                RosterMembership.ended_at.is_(None),
            )
        )
    }

    for player_id, row in open_rows.items():
        if player_id not in incoming:
            row.ended_at = now

    written = 0
    for player_id, entry in incoming.items():
        row = open_rows.get(player_id)
        if row is None:
            session.add(
                RosterMembership(
                    league_season_id=league_season.id,
                    team_season_id=team_season.id,
                    player_id=player_id,
                    slot=entry.get("slot"),
                    acquisition_type=entry.get("acquisition_type"),
                    started_at=now,
                    retrieved_at=now,
                )
            )
        else:
            row.slot = entry.get("slot") or row.slot
            row.retrieved_at = now
        written += 1

    session.flush()
    return written


#: Designations worth recording. "ACTIVE" is the absence of news, not news.
_MATERIAL_DESIGNATIONS = {"Q", "D", "O", "IR", "SUSP", "P", "DNP", "LP"}


def _sync_roster_status(
    session: Session,
    league_season: LeagueSeason,
    entries: list[dict],
    id_map: dict[str, str],
    provider_key: str,
) -> int:
    """Record platform injury designations as append-only status events.

    Only writes when the designation actually changed, so repeated syncs do not
    inflate the event log and `/pulse` materiality detection stays meaningful.
    """
    now = datetime.now(UTC)
    written = 0

    latest: dict[str, str] = {}
    for event in session.scalars(
        select(PlayerStatusEvent)
        .where(PlayerStatusEvent.season == league_season.season)
        .order_by(PlayerStatusEvent.retrieved_at)
    ):
        if event.designation:
            latest[event.player_id] = event.designation

    for entry in entries:
        player_id = id_map.get(str(entry.get("player_external_id")))
        if not player_id:
            continue
        designation = (entry.get("injury_status") or "").upper()
        if designation not in _MATERIAL_DESIGNATIONS:
            continue
        if latest.get(player_id) == designation:
            continue
        session.add(
            PlayerStatusEvent(
                player_id=player_id,
                season=league_season.season,
                week=league_season.current_week,
                event_type="injury",
                designation=designation,
                detail=f"reported by {provider_key}",
                retrieved_at=now,
                effective_at=now,
            )
        )
        latest[player_id] = designation
        written += 1

    session.flush()
    return written


def owner_team_season(session: Session, league_season_id: str) -> TeamSeason | None:
    """The team this system advises."""
    return session.scalar(
        select(TeamSeason)
        .join(Team, Team.id == TeamSeason.team_id)
        .where(TeamSeason.league_season_id == league_season_id, Team.is_owner_team.is_(True))
    )


def roster_players(session: Session, team_season_id: str) -> list[tuple[Player, RosterMembership]]:
    """Current roster: open membership intervals joined to canonical players."""
    rows = session.execute(
        select(Player, RosterMembership)
        .join(RosterMembership, RosterMembership.player_id == Player.id)
        .where(
            RosterMembership.team_season_id == team_season_id,
            RosterMembership.ended_at.is_(None),
        )
    ).all()
    return [(player, membership) for player, membership in rows]
