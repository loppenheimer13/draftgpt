"""Source providers, ingestion runs, and freshness state."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from draftgpt.database.models.base import Base, TimestampMixin, pk


class SourceProvider(Base, TimestampMixin):
    """A registered adapter. Rows are created by the provider registry on boot."""

    __tablename__ = "source_providers"
    __table_args__ = (UniqueConstraint("key", name="uq_source_providers_key"),)

    id: Mapped[str] = pk()
    key: Mapped[str] = mapped_column(String(64), nullable=False, doc="e.g. 'espn', 'fixture_proj'")
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    categories: Mapped[list] = mapped_column(
        nullable=False, default=list, doc="league|registry|projection|market|context|news"
    )
    capabilities: Mapped[list] = mapped_column(nullable=False, default=list)
    freshness_class: Mapped[str] = mapped_column(
        String(16), nullable=False, default="daily", doc="static|slow|daily|rapid|live"
    )
    license_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    requires_auth: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    rate_limit_per_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    raw_retention_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    config: Mapped[dict] = mapped_column(nullable=False, default=dict)


class IngestionRun(Base, TimestampMixin):
    """One attempt to pull one capability from one provider. Immutable once done."""

    __tablename__ = "ingestion_runs"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_ingestion_runs_idempotency_key"),
    )

    id: Mapped[str] = pk()
    provider_id: Mapped[str] = mapped_column(
        ForeignKey("source_providers.id"), nullable=False, index=True
    )
    capability: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="running", doc="running|success|partial|failed"
    )
    trigger: Mapped[str] = mapped_column(
        String(16), nullable=False, default="manual", doc="manual|scheduled|on_demand"
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    records_written: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_class: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Redacted request/response metadata only -- never credentials or full payloads.
    metrics: Mapped[dict] = mapped_column(nullable=False, default=dict)


class SourceFreshness(Base, TimestampMixin):
    """Normalized current view of "how stale is this input right now".

    One row per (provider, capability, scope). ``scope`` narrows to a league or
    week where a provider serves multiple scopes.
    """

    __tablename__ = "source_freshness"
    __table_args__ = (
        UniqueConstraint(
            "provider_id", "capability", "scope", name="uq_source_freshness_provider_id"
        ),
    )

    id: Mapped[str] = pk()
    provider_id: Mapped[str] = mapped_column(
        ForeignKey("source_providers.id"), nullable=False, index=True
    )
    capability: Mapped[str] = mapped_column(String(64), nullable=False)
    scope: Mapped[str] = mapped_column(String(128), nullable=False, default="global")
    freshness_class: Mapped[str] = mapped_column(String(16), nullable=False, default="daily")
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_effective_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_ingestion_run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    health: Mapped[str] = mapped_column(
        String(16), nullable=False, default="unknown", doc="healthy|degraded|failing|unknown"
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class ManualOverride(Base, TimestampMixin):
    """Audit trail for manual state correction (ops requirement, section 11)."""

    __tablename__ = "manual_overrides"

    id: Mapped[str] = pk()
    entity_table: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    field: Mapped[str] = mapped_column(String(64), nullable=False)
    previous_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor: Mapped[str] = mapped_column(String(64), nullable=False, default="owner")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    weight: Mapped[float | None] = mapped_column(Float, nullable=True)
