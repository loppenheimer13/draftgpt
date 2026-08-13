"""Draft state. Mutations are transactional and versioned (section 6)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from draftgpt.database.models.base import Base, ProvenanceMixin, TimestampMixin, pk


class Draft(Base, TimestampMixin, ProvenanceMixin):
    __tablename__ = "drafts"
    __table_args__ = (UniqueConstraint("league_season_id", name="uq_drafts_league_season_id"),)

    id: Mapped[str] = pk()
    league_season_id: Mapped[str] = mapped_column(ForeignKey("league_seasons.id"), nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    type: Mapped[str] = mapped_column(
        String(16), nullable=False, default="snake", doc="snake|linear|auction"
    )
    rounds: Mapped[int] = mapped_column(Integer, nullable=False)
    team_count: Mapped[int] = mapped_column(Integer, nullable=False)
    seconds_per_pick: Mapped[int | None] = mapped_column(Integer, nullable=True)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", doc="pending|in_progress|complete"
    )
    # Monotonic counter. Every applied mutation bumps it; recommendations pin it.
    current_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class DraftSlot(Base, TimestampMixin):
    """The precomputed pick order: which team owns overall pick N."""

    __tablename__ = "draft_slots"
    __table_args__ = (
        UniqueConstraint("draft_id", "overall_pick", name="uq_draft_slots_draft_id"),
    )

    id: Mapped[str] = pk()
    draft_id: Mapped[str] = mapped_column(ForeignKey("drafts.id"), nullable=False, index=True)
    overall_pick: Mapped[int] = mapped_column(Integer, nullable=False)
    round: Mapped[int] = mapped_column(Integer, nullable=False)
    pick_in_round: Mapped[int] = mapped_column(Integer, nullable=False)
    team_season_id: Mapped[str] = mapped_column(ForeignKey("team_seasons.id"), nullable=False)
    is_keeper_slot: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class DraftPick(Base, TimestampMixin, ProvenanceMixin):
    """An applied pick. Undo marks ``is_active=False`` rather than deleting, so
    the event log stays append-only and out-of-order events are detectable."""

    __tablename__ = "draft_picks"
    __table_args__ = (
        # A given overall pick may have at most one *active* row; enforced in
        # application logic plus this partial-safe uniqueness on the version.
        UniqueConstraint("draft_id", "overall_pick", "version", name="uq_draft_picks_draft_id"),
    )

    id: Mapped[str] = pk()
    draft_id: Mapped[str] = mapped_column(ForeignKey("drafts.id"), nullable=False, index=True)
    draft_slot_id: Mapped[str | None] = mapped_column(ForeignKey("draft_slots.id"), nullable=True)
    overall_pick: Mapped[int] = mapped_column(Integer, nullable=False)
    round: Mapped[int] = mapped_column(Integer, nullable=False)
    team_season_id: Mapped[str] = mapped_column(ForeignKey("team_seasons.id"), nullable=False)
    player_id: Mapped[str] = mapped_column(ForeignKey("players.id"), nullable=False, index=True)
    auction_price: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_keeper: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, doc="draft version that applied this"
    )
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="manual", doc="manual|provider"
    )


class DraftStateVersion(Base, TimestampMixin):
    """Append-only audit log of draft mutations; replay reconstructs any version."""

    __tablename__ = "draft_state_versions"
    __table_args__ = (
        UniqueConstraint("draft_id", "version", name="uq_draft_state_versions_draft_id"),
    )

    id: Mapped[str] = pk()
    draft_id: Mapped[str] = mapped_column(ForeignKey("drafts.id"), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    operation: Mapped[str] = mapped_column(
        String(24), nullable=False, doc="initialize|pick|undo|resync|correction"
    )
    payload: Mapped[dict] = mapped_column(nullable=False, default=dict)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    actor: Mapped[str] = mapped_column(String(32), nullable=False, default="owner")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class AdpSnapshot(Base, TimestampMixin, ProvenanceMixin):
    """Average draft position with dispersion, used for survival probability."""

    __tablename__ = "adp_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "player_id", "season", "provider_id", "retrieved_at", name="uq_adp_snapshots_player_id"
        ),
    )

    id: Mapped[str] = pk()
    player_id: Mapped[str] = mapped_column(ForeignKey("players.id"), nullable=False, index=True)
    season: Mapped[int] = mapped_column(Integer, nullable=False)
    scoring_format: Mapped[str] = mapped_column(String(16), nullable=False, default="ppr")
    team_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    adp: Mapped[float] = mapped_column(Float, nullable=False)
    adp_stdev: Mapped[float | None] = mapped_column(
        Float, nullable=True, doc="drives the survival-probability model"
    )
    earliest_pick: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latest_pick: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sample_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
