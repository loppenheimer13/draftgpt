"""Show runs and the audio segments they produce.

One run is one family's episode for one day. The brief that produced it is
persisted alongside the finished script, so a parent can always see exactly
which facts went into what their child heard.
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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from huddle.database.models.base import Base, TimestampMixin, pk


class ShowRun(Base, TimestampMixin):
    __tablename__ = "show_runs"
    __table_args__ = (
        UniqueConstraint("family_id", "show_date", name="uq_show_runs_family_id"),
        Index("ix_show_runs_status", "status"),
    )

    id: Mapped[str] = pk()
    family_id: Mapped[str] = mapped_column(
        ForeignKey("families.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: The family's *local* date, so one show per calendar day per household
    #: regardless of when the scheduler happened to fire.
    show_date: Mapped[date] = mapped_column(Date, nullable=False)

    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    calculation_version: Mapped[str] = mapped_column(String(32), nullable=False)
    audio_pipeline: Mapped[str] = mapped_column(String(16), nullable=False, default="labs")

    #: The full structured brief, hashed for change detection. Two consecutive
    #: days with an identical hash mean nothing happened worth republishing.
    brief: Mapped[dict] = mapped_column(nullable=False, default=dict)
    brief_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    card_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Labs pipeline only: the asynchronous synthesis job to poll.
    labs_job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    #: Estimated from the script when written; replaced with the measured value
    #: once the upload pipeline has real audio.
    estimated_minutes: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)

    #: What the safety filter removed today, kept for the parent dashboard.
    blocked_stories: Mapped[list] = mapped_column(nullable=False, default=list)
    #: Whether the narration model rewrote any segment, or was skipped.
    narration_used: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_class: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    warnings: Mapped[list] = mapped_column(nullable=False, default=list)

    segments: Mapped[list[ShowSegmentRow]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="ShowSegmentRow.sort_order"
    )


class ShowSegmentRow(Base, TimestampMixin):
    """One track. Chapters group segments by show segment at publish time."""

    __tablename__ = "show_segments"
    __table_args__ = (
        UniqueConstraint("run_id", "track_key", "chapter_key", name="uq_show_segments_run_id"),
        Index("ix_show_segments_run_order", "run_id", "sort_order"),
    )

    id: Mapped[str] = pk()
    run_id: Mapped[str] = mapped_column(
        ForeignKey("show_runs.id", ondelete="CASCADE"), nullable=False
    )
    segment: Mapped[str] = mapped_column(String(32), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Yoto track keys are positional strings ("01", "02", ...).
    track_key: Mapped[str] = mapped_column(String(8), nullable=False)
    chapter_key: Mapped[str] = mapped_column(String(8), nullable=False, default="01")
    title: Mapped[str] = mapped_column(String(200), nullable=False)

    #: Which host reads this track. Drives the voice used at synthesis.
    host: Mapped[str] = mapped_column(String(16), nullable=False, default="nova")
    voice_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    #: What gets spoken, after safety re-check and any narration pass.
    script: Mapped[str] = mapped_column(Text, nullable=False)
    #: The deterministic text, before narration. Kept so a parent can compare
    #: what the model changed against what the templates wrote.
    source_script: Mapped[str | None] = mapped_column(Text, nullable=True)
    char_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: Upload pipeline only -- Labs fills these in on Yoto's side.
    audio_sha256: Mapped[str | None] = mapped_column(String(128), nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    file_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    audio_format: Mapped[str | None] = mapped_column(String(16), nullable=True)
    icon: Mapped[str | None] = mapped_column(String(128), nullable=True)

    #: Which facts this segment is allowed to assert, by key. The narration
    #: model may rephrase these and nothing else.
    material_inputs: Mapped[list] = mapped_column(nullable=False, default=list)

    run: Mapped[ShowRun] = relationship(back_populates="segments")
