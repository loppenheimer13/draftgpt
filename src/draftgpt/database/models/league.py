"""League configuration, teams, rosters, matchups, and transactions."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from draftgpt.database.models.base import Base, ProvenanceMixin, TimestampMixin, pk


class User(Base, TimestampMixin):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("handle", name="uq_users_handle"),)

    id: Mapped[str] = pk()
    handle: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    email: Mapped[str | None] = mapped_column(String(254), nullable=True)
    is_owner: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, doc="the human this system advises"
    )
    risk_preference: Mapped[str] = mapped_column(
        String(16), nullable=False, default="balanced", doc="floor|balanced|ceiling"
    )


class League(Base, TimestampMixin):
    __tablename__ = "leagues"
    __table_args__ = (
        UniqueConstraint("provider_key", "external_id", name="uq_leagues_provider_key"),
    )

    id: Mapped[str] = pk()
    provider_key: Mapped[str] = mapped_column(String(64), nullable=False)
    external_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    format: Mapped[str] = mapped_column(
        String(24), nullable=False, default="redraft", doc="redraft|keeper|dynasty|bestball"
    )


class LeagueSeason(Base, TimestampMixin, ProvenanceMixin):
    __tablename__ = "league_seasons"
    __table_args__ = (UniqueConstraint("league_id", "season", name="uq_league_seasons_league_id"),)

    id: Mapped[str] = pk()
    league_id: Mapped[str] = mapped_column(ForeignKey("leagues.id"), nullable=False, index=True)
    season: Mapped[int] = mapped_column(Integer, nullable=False)
    team_count: Mapped[int] = mapped_column(Integer, nullable=False)
    current_week: Mapped[int | None] = mapped_column(Integer, nullable=True)
    regular_season_weeks: Mapped[int] = mapped_column(Integer, nullable=False, default=14)
    playoff_weeks: Mapped[list] = mapped_column(nullable=False, default=list)
    playoff_team_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    trade_deadline_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="pre_draft", doc="pre_draft|drafting|in_season|complete"
    )


class LeagueRules(Base, TimestampMixin, ProvenanceMixin):
    """Scoring and roster rules. Versioned: a mid-season settings change creates
    a new row rather than mutating history, so old recommendations replay."""

    __tablename__ = "league_rules"
    __table_args__ = (
        UniqueConstraint("league_season_id", "version", name="uq_league_rules_league_season_id"),
    )

    id: Mapped[str] = pk()
    league_season_id: Mapped[str] = mapped_column(
        ForeignKey("league_seasons.id"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    effective_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # {canonical_stat_key: points_per_unit}, plus threshold bonuses under "bonuses".
    scoring: Mapped[dict] = mapped_column(nullable=False, default=dict)
    # Waiver / FAAB
    waiver_type: Mapped[str] = mapped_column(
        String(24), nullable=False, default="faab", doc="faab|rolling|reverse_standings|none"
    )
    faab_budget: Mapped[int | None] = mapped_column(Integer, nullable=True)
    waiver_process_days: Mapped[list] = mapped_column(nullable=False, default=list)
    waiver_tiebreak: Mapped[str | None] = mapped_column(String(48), nullable=True)
    # Roster limits
    roster_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ir_slots: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    trade_review_type: Mapped[str | None] = mapped_column(String(24), nullable=True)
    raw_settings: Mapped[dict] = mapped_column(nullable=False, default=dict)


class RosterSlot(Base, TimestampMixin):
    """A startable slot definition, e.g. FLEX accepting RB/WR/TE.

    ``ordinal`` disambiguates the two RB slots. ``count`` is always 1 per row so
    the optimizer treats slots as distinct assignment targets.
    """

    __tablename__ = "roster_slots"
    __table_args__ = (
        UniqueConstraint(
            "league_rules_id", "slot", "ordinal", name="uq_roster_slots_league_rules_id"
        ),
    )

    id: Mapped[str] = pk()
    league_rules_id: Mapped[str] = mapped_column(
        ForeignKey("league_rules.id"), nullable=False, index=True
    )
    slot: Mapped[str] = mapped_column(
        String(16), nullable=False, doc="QB|RB|WR|TE|FLEX|SFLEX|K|DST|BE|IR"
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    eligible_positions: Mapped[list] = mapped_column(nullable=False, default=list)
    is_starting: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class Team(Base, TimestampMixin):
    __tablename__ = "teams"
    __table_args__ = (
        UniqueConstraint("league_id", "external_id", name="uq_teams_league_id"),
    )

    id: Mapped[str] = pk()
    league_id: Mapped[str] = mapped_column(ForeignKey("leagues.id"), nullable=False, index=True)
    external_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    owner_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    is_owner_team: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, doc="true for the team this system advises"
    )


class TeamSeason(Base, TimestampMixin, ProvenanceMixin):
    __tablename__ = "team_seasons"
    __table_args__ = (
        UniqueConstraint("team_id", "league_season_id", name="uq_team_seasons_team_id"),
    )

    id: Mapped[str] = pk()
    team_id: Mapped[str] = mapped_column(ForeignKey("teams.id"), nullable=False, index=True)
    league_season_id: Mapped[str] = mapped_column(
        ForeignKey("league_seasons.id"), nullable=False, index=True
    )
    draft_position: Mapped[int | None] = mapped_column(Integer, nullable=True)
    wins: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    losses: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ties: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    points_for: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    points_against: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    faab_remaining: Mapped[int | None] = mapped_column(Integer, nullable=True)
    waiver_priority: Mapped[int | None] = mapped_column(Integer, nullable=True)


class RosterMembership(Base, TimestampMixin, ProvenanceMixin):
    """Who rosters whom, as an interval. Open interval (`ended_at IS NULL`) is
    the current roster; closed intervals reconstruct any past week."""

    __tablename__ = "roster_memberships"
    __table_args__ = (
        Index("ix_roster_memberships_current", "league_season_id", "player_id", "ended_at"),
    )

    id: Mapped[str] = pk()
    league_season_id: Mapped[str] = mapped_column(
        ForeignKey("league_seasons.id"), nullable=False, index=True
    )
    team_season_id: Mapped[str] = mapped_column(
        ForeignKey("team_seasons.id"), nullable=False, index=True
    )
    player_id: Mapped[str] = mapped_column(ForeignKey("players.id"), nullable=False, index=True)
    slot: Mapped[str | None] = mapped_column(
        String(16), nullable=True, doc="platform's currently-configured slot"
    )
    acquisition_type: Mapped[str | None] = mapped_column(
        String(24), nullable=True, doc="draft|waiver|free_agent|trade"
    )
    acquisition_cost: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Matchup(Base, TimestampMixin, ProvenanceMixin):
    __tablename__ = "matchups"
    __table_args__ = (
        UniqueConstraint(
            "league_season_id", "week", "home_team_season_id", name="uq_matchups_league_season_id"
        ),
    )

    id: Mapped[str] = pk()
    league_season_id: Mapped[str] = mapped_column(
        ForeignKey("league_seasons.id"), nullable=False, index=True
    )
    week: Mapped[int] = mapped_column(Integer, nullable=False)
    home_team_season_id: Mapped[str] = mapped_column(ForeignKey("team_seasons.id"), nullable=False)
    away_team_season_id: Mapped[str | None] = mapped_column(
        ForeignKey("team_seasons.id"), nullable=True, doc="null for a bye"
    )
    home_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    away_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_playoff: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="scheduled")


class StandingsSnapshot(Base, TimestampMixin, ProvenanceMixin):
    __tablename__ = "standings_snapshots"

    id: Mapped[str] = pk()
    league_season_id: Mapped[str] = mapped_column(
        ForeignKey("league_seasons.id"), nullable=False, index=True
    )
    week: Mapped[int] = mapped_column(Integer, nullable=False)
    standings: Mapped[list] = mapped_column(nullable=False, default=list)


class Transaction(Base, TimestampMixin, ProvenanceMixin):
    __tablename__ = "transactions"
    __table_args__ = (
        UniqueConstraint("provider_key", "external_id", name="uq_transactions_provider_key"),
        Index("ix_transactions_season_time", "league_season_id", "processed_at"),
    )

    id: Mapped[str] = pk()
    league_season_id: Mapped[str] = mapped_column(ForeignKey("league_seasons.id"), nullable=False)
    provider_key: Mapped[str] = mapped_column(String(64), nullable=False)
    external_id: Mapped[str] = mapped_column(String(96), nullable=False)
    type: Mapped[str] = mapped_column(
        String(24), nullable=False, doc="add|drop|add_drop|trade|waiver|draft"
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="executed")
    team_season_id: Mapped[str | None] = mapped_column(ForeignKey("team_seasons.id"), nullable=True)
    bid_amount: Mapped[int | None] = mapped_column(Integer, nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    items: Mapped[list] = mapped_column(
        nullable=False, default=list, doc="[{player_id, action, from_team_season_id, to_...}]"
    )


class WaiverStateSnapshot(Base, TimestampMixin, ProvenanceMixin):
    __tablename__ = "waiver_state_snapshots"

    id: Mapped[str] = pk()
    league_season_id: Mapped[str] = mapped_column(
        ForeignKey("league_seasons.id"), nullable=False, index=True
    )
    week: Mapped[int | None] = mapped_column(Integer, nullable=True)
    next_process_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    team_state: Mapped[list] = mapped_column(
        nullable=False, default=list, doc="[{team_season_id, faab_remaining, waiver_priority}]"
    )


class PulseAcknowledgement(Base, TimestampMixin):
    """Watermark for `/pulse`: everything effective after this was not yet shown."""

    __tablename__ = "pulse_acknowledgements"

    id: Mapped[str] = pk()
    league_season_id: Mapped[str] = mapped_column(
        ForeignKey("league_seasons.id"), nullable=False, index=True
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    acknowledged_through: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
