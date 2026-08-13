"""Recommendation and pulse history.

Every command run is persisted with the decision snapshot it read and the
calculation version that produced it, so prior advice can be replayed and
backtested without leaking future information (section 10).
"""

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

from draftgpt.database.models.base import Base, TimestampMixin, pk


class DecisionSnapshot(Base, TimestampMixin):
    """The immutable, hashed set of inputs a recommendation was computed from."""

    __tablename__ = "decision_snapshots"
    __table_args__ = (Index("ix_decision_snapshots_league_time", "league_season_id", "created_at"),)

    id: Mapped[str] = pk()
    league_season_id: Mapped[str] = mapped_column(ForeignKey("league_seasons.id"), nullable=False)
    season: Mapped[int] = mapped_column(Integer, nullable=False)
    week: Mapped[int | None] = mapped_column(Integer, nullable=True)
    as_of: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, doc="no input effective after this was used"
    )
    draft_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    league_rules_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # Provider/capability -> effective + retrieved timestamps at read time.
    source_versions: Mapped[dict] = mapped_column(nullable=False, default=dict)
    payload: Mapped[dict] = mapped_column(
        nullable=False, default=dict, doc="serialized snapshot for exact replay"
    )


class RecommendationRun(Base, TimestampMixin):
    __tablename__ = "recommendation_runs"
    __table_args__ = (Index("ix_recommendation_runs_cmd_time", "command", "generated_at"),)

    id: Mapped[str] = pk()
    command: Mapped[str] = mapped_column(
        String(24), nullable=False, doc="draft|pulse|roster|waiver|trade"
    )
    subcommand: Mapped[str | None] = mapped_column(String(32), nullable=True)
    league_season_id: Mapped[str] = mapped_column(ForeignKey("league_seasons.id"), nullable=False)
    team_season_id: Mapped[str | None] = mapped_column(ForeignKey("team_seasons.id"), nullable=True)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("decision_snapshots.id"), nullable=False)
    calculation_version: Mapped[str] = mapped_column(String(32), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    week: Mapped[int | None] = mapped_column(Integer, nullable=True)
    request: Mapped[dict] = mapped_column(nullable=False, default=dict)
    # Full structured contract (section 8) exactly as returned to the caller.
    response: Mapped[dict] = mapped_column(nullable=False, default=dict)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    freshness_status: Mapped[str] = mapped_column(String(16), nullable=False, default="fresh")
    degraded_reasons: Mapped[list] = mapped_column(nullable=False, default=list)
    check_again_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    explanation_model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    explanation: Mapped[str | None] = mapped_column(Text, nullable=True)


class RecommendationCandidate(Base, TimestampMixin):
    """Structured evidence per candidate -- not only prose (section 6)."""

    __tablename__ = "recommendation_candidates"
    __table_args__ = (
        UniqueConstraint("run_id", "rank", name="uq_recommendation_candidates_run_id"),
    )

    id: Mapped[str] = pk()
    run_id: Mapped[str] = mapped_column(
        ForeignKey("recommendation_runs.id"), nullable=False, index=True
    )
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    is_recommended: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    subject_type: Mapped[str] = mapped_column(
        String(24), nullable=False, doc="player|lineup|claim|trade_package"
    )
    subject_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    projected_points: Mapped[float | None] = mapped_column(Float, nullable=True)
    floor_points: Mapped[float | None] = mapped_column(Float, nullable=True)
    ceiling_points: Mapped[float | None] = mapped_column(Float, nullable=True)
    value_over_replacement: Mapped[float | None] = mapped_column(Float, nullable=True)
    survival_probability: Mapped[float | None] = mapped_column(Float, nullable=True)
    tier: Mapped[int | None] = mapped_column(Integer, nullable=True)
    evidence: Mapped[dict] = mapped_column(nullable=False, default=dict)


class PulseRun(Base, TimestampMixin):
    __tablename__ = "pulse_runs"

    id: Mapped[str] = pk()
    league_season_id: Mapped[str] = mapped_column(ForeignKey("league_seasons.id"), nullable=False)
    team_season_id: Mapped[str | None] = mapped_column(ForeignKey("team_seasons.id"), nullable=True)
    snapshot_id: Mapped[str | None] = mapped_column(
        ForeignKey("decision_snapshots.id"), nullable=True
    )
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="standard")
    since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    item_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class PulseItem(Base, TimestampMixin):
    __tablename__ = "pulse_items"
    __table_args__ = (
        # Dedup key: the same underlying change must not resurface next pulse.
        UniqueConstraint("league_season_id", "dedup_key", name="uq_pulse_items_league_season_id"),
    )

    id: Mapped[str] = pk()
    pulse_run_id: Mapped[str] = mapped_column(
        ForeignKey("pulse_runs.id"), nullable=False, index=True
    )
    league_season_id: Mapped[str] = mapped_column(ForeignKey("league_seasons.id"), nullable=False)
    dedup_key: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[str] = mapped_column(
        String(32), nullable=False, doc="injury|role|projection|waiver|transaction|trade|deadline"
    )
    urgency: Mapped[str] = mapped_column(
        String(16), nullable=False, default="normal", doc="critical|high|normal|low"
    )
    headline: Mapped[str] = mapped_column(String(400), nullable=False)
    impact: Mapped[str | None] = mapped_column(Text, nullable=True)
    recommended_action: Mapped[str | None] = mapped_column(Text, nullable=True)
    deeper_command: Mapped[str | None] = mapped_column(String(64), nullable=True)
    player_ids: Mapped[list] = mapped_column(nullable=False, default=list)
    evidence: Mapped[dict] = mapped_column(nullable=False, default=dict)
    materiality_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    effective_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
