"""Projections, rankings, and tiers. Never overwritten in place (section 6)."""

from __future__ import annotations

from sqlalchemy import Float, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from draftgpt.database.models.base import Base, ProvenanceMixin, TimestampMixin, pk


class Projection(Base, TimestampMixin, ProvenanceMixin):
    """A single provider's projection for one player/scope.

    ``stat_line`` holds canonical stat keys so league-specific scoring can be
    applied at read time; ``projected_points`` is a convenience cache of that
    scoring under a named ruleset and may be null until computed.
    """

    __tablename__ = "projections"
    __table_args__ = (
        UniqueConstraint(
            "player_id", "season", "week", "scope", "provider_id", "retrieved_at",
            name="uq_projections_player_id",
        ),
        Index("ix_projections_lookup", "season", "week", "scope", "player_id"),
    )

    id: Mapped[str] = pk()
    player_id: Mapped[str] = mapped_column(ForeignKey("players.id"), nullable=False, index=True)
    season: Mapped[int] = mapped_column(Integer, nullable=False)
    week: Mapped[int | None] = mapped_column(Integer, nullable=True, doc="null for season scopes")
    scope: Mapped[str] = mapped_column(
        String(16), nullable=False, default="week", doc="week|season|ros"
    )
    stat_line: Mapped[dict] = mapped_column(nullable=False, default=dict)
    projected_points: Mapped[float | None] = mapped_column(Float, nullable=True)
    floor_points: Mapped[float | None] = mapped_column(Float, nullable=True)
    ceiling_points: Mapped[float | None] = mapped_column(Float, nullable=True)
    stdev_points: Mapped[float | None] = mapped_column(Float, nullable=True)
    games_remaining: Mapped[int | None] = mapped_column(Integer, nullable=True)
    scoring_ruleset: Mapped[str | None] = mapped_column(
        String(64), nullable=True, doc="which league_rules version priced this, if cached"
    )


class Ranking(Base, TimestampMixin, ProvenanceMixin):
    __tablename__ = "rankings"
    __table_args__ = (
        UniqueConstraint(
            "player_id", "season", "week", "scope", "provider_id", "retrieved_at",
            name="uq_rankings_player_id",
        ),
    )

    id: Mapped[str] = pk()
    player_id: Mapped[str] = mapped_column(ForeignKey("players.id"), nullable=False, index=True)
    season: Mapped[int] = mapped_column(Integer, nullable=False)
    week: Mapped[int | None] = mapped_column(Integer, nullable=True)
    scope: Mapped[str] = mapped_column(String(16), nullable=False, default="season")
    scoring_format: Mapped[str] = mapped_column(String(16), nullable=False, default="ppr")
    overall_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    position: Mapped[str] = mapped_column(String(8), nullable=False)
    position_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expert_consensus_stdev: Mapped[float | None] = mapped_column(Float, nullable=True)


class Tier(Base, TimestampMixin, ProvenanceMixin):
    """Positional tier assignment. Cliff detection reads consecutive tiers."""

    __tablename__ = "tiers"
    __table_args__ = (
        UniqueConstraint(
            "player_id", "season", "week", "provider_id", "retrieved_at", name="uq_tiers_player_id"
        ),
    )

    id: Mapped[str] = pk()
    player_id: Mapped[str] = mapped_column(ForeignKey("players.id"), nullable=False, index=True)
    season: Mapped[int] = mapped_column(Integer, nullable=False)
    week: Mapped[int | None] = mapped_column(Integer, nullable=True)
    position: Mapped[str] = mapped_column(String(8), nullable=False)
    tier: Mapped[int] = mapped_column(Integer, nullable=False)
    tier_label: Mapped[str | None] = mapped_column(String(64), nullable=True)


class MarketTrend(Base, TimestampMixin, ProvenanceMixin):
    """Roster percentage and add/drop velocity -> waiver demand estimation."""

    __tablename__ = "market_trends"
    __table_args__ = (
        UniqueConstraint(
            "player_id", "season", "week", "provider_id", "retrieved_at",
            name="uq_market_trends_player_id",
        ),
    )

    id: Mapped[str] = pk()
    player_id: Mapped[str] = mapped_column(ForeignKey("players.id"), nullable=False, index=True)
    season: Mapped[int] = mapped_column(Integer, nullable=False)
    week: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rostered_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    started_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    adds_24h: Mapped[int | None] = mapped_column(Integer, nullable=True)
    drops_24h: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rostered_pct_change: Mapped[float | None] = mapped_column(Float, nullable=True)
