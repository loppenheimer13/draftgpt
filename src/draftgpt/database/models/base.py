"""Declarative base and shared column conventions.

Portability note: every type used here has an identical spelling on SQLite and
PostgreSQL. Canonical IDs are ULID-ish strings rather than native UUID columns
so a single migration script targets both backends.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, Float, MetaData, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import JSON

# Explicit naming convention so Alembic can autogenerate reversible constraint
# operations on SQLite (which requires batch mode / named constraints).
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {dict: JSON, list: JSON, float: Float}


def new_id() -> str:
    """Canonical internal identifier. Never a provider identifier."""
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(UTC)


def pk() -> Mapped[str]:
    return mapped_column(String(32), primary_key=True, default=new_id)


class TimestampMixin:
    """Row-level audit timestamps. Distinct from data provenance below."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class ProvenanceMixin:
    """Principle 3: source and timestamp everything.

    ``retrieved_at`` is when we fetched it. ``effective_at`` is when the fact
    became true in the world (a Friday practice report is effective Friday even
    if ingested Saturday). Freshness math uses ``effective_at`` where present.
    """

    provider_id: Mapped[str | None] = mapped_column(
        String(32), nullable=True, index=True, doc="FK-ish to source_providers.id"
    )
    ingestion_run_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    retrieved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    effective_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
