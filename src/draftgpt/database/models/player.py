"""Canonical player identity, NFL structure, and player context snapshots.

Identity rule: `players.id` is ours. Provider keys live only in
`player_provider_ids`. Nothing downstream may join on a provider key.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
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


class NflTeam(Base, TimestampMixin):
    __tablename__ = "nfl_teams"
    __table_args__ = (UniqueConstraint("abbreviation", name="uq_nfl_teams_abbreviation"),)

    id: Mapped[str] = pk()
    abbreviation: Mapped[str] = mapped_column(String(8), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    conference: Mapped[str | None] = mapped_column(String(4), nullable=True)
    division: Mapped[str | None] = mapped_column(String(16), nullable=True)
    bye_week_by_season: Mapped[dict] = mapped_column(nullable=False, default=dict)


class NflGame(Base, TimestampMixin, ProvenanceMixin):
    __tablename__ = "nfl_games"
    __table_args__ = (
        UniqueConstraint("season", "week", "home_team_id", name="uq_nfl_games_season"),
        Index("ix_nfl_games_season_week", "season", "week"),
    )

    id: Mapped[str] = pk()
    season: Mapped[int] = mapped_column(Integer, nullable=False)
    week: Mapped[int] = mapped_column(Integer, nullable=False)
    home_team_id: Mapped[str] = mapped_column(ForeignKey("nfl_teams.id"), nullable=False)
    away_team_id: Mapped[str] = mapped_column(ForeignKey("nfl_teams.id"), nullable=False)
    kickoff_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    venue: Mapped[str | None] = mapped_column(String(128), nullable=True)
    is_dome: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="scheduled", doc="scheduled|in_progress|final"
    )
    home_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    away_score: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Player(Base, TimestampMixin, ProvenanceMixin):
    """Canonical player. Team/position here are the *current* view; the
    season-effective history lives in `player_season_eligibility`."""

    __tablename__ = "players"
    __table_args__ = (Index("ix_players_normalized_name", "normalized_name"),)

    id: Mapped[str] = pk()
    full_name: Mapped[str] = mapped_column(String(128), nullable=False)
    normalized_name: Mapped[str] = mapped_column(
        String(128), nullable=False, doc="lowercased, punctuation- and suffix-stripped"
    )
    first_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    primary_position: Mapped[str] = mapped_column(String(8), nullable=False)
    nfl_team_id: Mapped[str | None] = mapped_column(ForeignKey("nfl_teams.id"), nullable=True)
    birth_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    jersey_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="active", doc="active|inactive|ir|suspended|retired|fa"
    )
    is_team_defense: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class PlayerAlias(Base, TimestampMixin):
    """Known alternate spellings. Used by the resolver before fuzzy matching."""

    __tablename__ = "player_aliases"
    __table_args__ = (
        UniqueConstraint(
            "normalized_alias", "player_id", name="uq_player_aliases_normalized_alias"
        ),
    )

    id: Mapped[str] = pk()
    player_id: Mapped[str] = mapped_column(ForeignKey("players.id"), nullable=False, index=True)
    alias: Mapped[str] = mapped_column(String(128), nullable=False)
    normalized_alias: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    source: Mapped[str | None] = mapped_column(String(64), nullable=True)


class PlayerProviderId(Base, TimestampMixin):
    """The only legal bridge between a provider's key space and ours."""

    __tablename__ = "player_provider_ids"
    __table_args__ = (
        UniqueConstraint("provider_key", "external_id", name="uq_player_provider_ids_provider_key"),
        Index("ix_player_provider_ids_player", "player_id", "provider_key"),
    )

    id: Mapped[str] = pk()
    player_id: Mapped[str] = mapped_column(ForeignKey("players.id"), nullable=False)
    provider_key: Mapped[str] = mapped_column(String(64), nullable=False)
    external_id: Mapped[str] = mapped_column(String(64), nullable=False)
    external_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    match_method: Mapped[str] = mapped_column(
        String(24), nullable=False, default="exact", doc="exact|alias|fuzzy|manual"
    )
    match_confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    needs_review: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class PlayerSeasonEligibility(Base, TimestampMixin, ProvenanceMixin):
    """Position eligibility preserved by season + effective time (section 6)."""

    __tablename__ = "player_season_eligibility"
    __table_args__ = (
        UniqueConstraint(
            "player_id", "season", "effective_at", name="uq_player_season_eligibility_player_id"
        ),
    )

    id: Mapped[str] = pk()
    player_id: Mapped[str] = mapped_column(ForeignKey("players.id"), nullable=False, index=True)
    season: Mapped[int] = mapped_column(Integer, nullable=False)
    nfl_team_id: Mapped[str | None] = mapped_column(ForeignKey("nfl_teams.id"), nullable=True)
    positions: Mapped[list] = mapped_column(nullable=False, default=list)


class PlayerStatusEvent(Base, TimestampMixin, ProvenanceMixin):
    """Injury designation, practice participation, inactives. Append-only."""

    __tablename__ = "player_status_events"
    __table_args__ = (Index("ix_player_status_events_player_eff", "player_id", "effective_at"),)

    id: Mapped[str] = pk()
    player_id: Mapped[str] = mapped_column(ForeignKey("players.id"), nullable=False)
    season: Mapped[int] = mapped_column(Integer, nullable=False)
    week: Mapped[int | None] = mapped_column(Integer, nullable=True)
    event_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        doc="injury|practice|inactive|activation|suspension|depth_change",
    )
    designation: Mapped[str | None] = mapped_column(
        String(24), nullable=True, doc="Q|D|O|IR|PUP|DNP|LP|FP|ACTIVE"
    )
    body_part: Mapped[str | None] = mapped_column(String(48), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)


class DepthChartSnapshot(Base, TimestampMixin, ProvenanceMixin):
    __tablename__ = "depth_chart_snapshots"
    __table_args__ = (
        Index("ix_depth_chart_snapshots_team_eff", "nfl_team_id", "season", "week"),
    )

    id: Mapped[str] = pk()
    nfl_team_id: Mapped[str] = mapped_column(ForeignKey("nfl_teams.id"), nullable=False)
    season: Mapped[int] = mapped_column(Integer, nullable=False)
    week: Mapped[int | None] = mapped_column(Integer, nullable=True)
    position: Mapped[str] = mapped_column(String(8), nullable=False)
    player_id: Mapped[str] = mapped_column(ForeignKey("players.id"), nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str | None] = mapped_column(String(32), nullable=True)


class UsageSnapshot(Base, TimestampMixin, ProvenanceMixin):
    """Opportunity metrics. Volume predicts better than efficiency."""

    __tablename__ = "usage_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "player_id", "season", "week", "provider_id", name="uq_usage_snapshots_player_id"
        ),
    )

    id: Mapped[str] = pk()
    player_id: Mapped[str] = mapped_column(ForeignKey("players.id"), nullable=False, index=True)
    season: Mapped[int] = mapped_column(Integer, nullable=False)
    week: Mapped[int | None] = mapped_column(Integer, nullable=True)
    snap_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    route_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_share: Mapped[float | None] = mapped_column(Float, nullable=True)
    carry_share: Mapped[float | None] = mapped_column(Float, nullable=True)
    red_zone_touches: Mapped[int | None] = mapped_column(Integer, nullable=True)
    air_yards_share: Mapped[float | None] = mapped_column(Float, nullable=True)
    raw: Mapped[dict] = mapped_column(nullable=False, default=dict)


class WeatherSnapshot(Base, TimestampMixin, ProvenanceMixin):
    __tablename__ = "weather_snapshots"

    id: Mapped[str] = pk()
    nfl_game_id: Mapped[str] = mapped_column(ForeignKey("nfl_games.id"), nullable=False, index=True)
    temperature_f: Mapped[float | None] = mapped_column(Float, nullable=True)
    wind_mph: Mapped[float | None] = mapped_column(Float, nullable=True)
    precipitation_chance: Mapped[float | None] = mapped_column(Float, nullable=True)
    conditions: Mapped[str | None] = mapped_column(String(64), nullable=True)


class MarketContextSnapshot(Base, TimestampMixin, ProvenanceMixin):
    """Betting totals/spreads -> implied team totals, a strong game-script prior."""

    __tablename__ = "market_context_snapshots"

    id: Mapped[str] = pk()
    nfl_game_id: Mapped[str] = mapped_column(ForeignKey("nfl_games.id"), nullable=False, index=True)
    total: Mapped[float | None] = mapped_column(Float, nullable=True)
    home_spread: Mapped[float | None] = mapped_column(Float, nullable=True)
    home_implied_total: Mapped[float | None] = mapped_column(Float, nullable=True)
    away_implied_total: Mapped[float | None] = mapped_column(Float, nullable=True)


class NewsItem(Base, TimestampMixin, ProvenanceMixin):
    __tablename__ = "news_items"
    __table_args__ = (
        UniqueConstraint("provider_key", "external_id", name="uq_news_items_provider_key"),
    )

    id: Mapped[str] = pk()
    provider_key: Mapped[str] = mapped_column(String(64), nullable=False)
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    headline: Mapped[str] = mapped_column(String(400), nullable=False)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    url: Mapped[str | None] = mapped_column(String(600), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    player_ids: Mapped[list] = mapped_column(nullable=False, default=list)
    impact: Mapped[str | None] = mapped_column(String(16), nullable=True)
