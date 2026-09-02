"""Ingestion run lifecycle and freshness bookkeeping.

Every external fetch goes through :func:`ingestion_run`, which guarantees that
whatever happens -- success, partial, or failure -- the run is recorded and
``source_freshness`` reflects reality. A command can then honestly report "the
injury feed last succeeded 40 minutes ago" instead of silently using stale data.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from huddle.database.models import IngestionRun, SourceFreshness, SourceProvider
from huddle.domain.freshness import FreshnessInput
from huddle.providers.base import Provider, ProviderSpec

logger = logging.getLogger(__name__)


@dataclass
class RunHandle:
    """Mutable handle the caller fills in as the run progresses."""

    run: IngestionRun
    records_written: int = 0
    partial: bool = False
    notes: list[str] = field(default_factory=list)
    effective_at: datetime | None = None

    def wrote(self, count: int) -> None:
        self.records_written += count


def upsert_provider(session: Session, spec: ProviderSpec) -> SourceProvider:
    """Register (or refresh) an adapter's declaration in the database."""
    provider = session.scalar(select(SourceProvider).where(SourceProvider.key == spec.key))
    if provider is None:
        provider = SourceProvider(key=spec.key)
        session.add(provider)
    provider.display_name = spec.display_name
    provider.categories = list(spec.categories)
    provider.capabilities = [str(c) for c in spec.capabilities]
    provider.freshness_class = str(spec.freshness_class)
    provider.license_note = spec.license_note
    provider.requires_auth = spec.requires_auth
    provider.rate_limit_per_min = spec.rate_limit_per_min
    provider.raw_retention_days = spec.raw_retention_days
    session.flush()
    return provider


def sync_provider_registry(session: Session, specs: list[ProviderSpec]) -> list[SourceProvider]:
    return [upsert_provider(session, spec) for spec in specs]


@contextmanager
def ingestion_run(
    session: Session,
    provider: SourceProvider,
    capability: str,
    *,
    scope: str = "global",
    trigger: str = "manual",
    idempotency_key: str | None = None,
    freshness_class: str | None = None,
) -> Iterator[RunHandle]:
    """Record one ingestion attempt and update freshness on the way out.

    Exceptions are recorded and re-raised: callers decide whether a failed
    non-critical source should abort their work (it usually should not).
    """
    started = datetime.now(UTC)
    run = IngestionRun(
        provider_id=provider.id,
        capability=capability,
        idempotency_key=idempotency_key,
        status="running",
        trigger=trigger,
        started_at=started,
    )
    session.add(run)
    session.flush()

    handle = RunHandle(run=run)
    try:
        yield handle
    except Exception as exc:
        run.status = "failed"
        run.finished_at = datetime.now(UTC)
        run.error_class = type(exc).__name__
        # Message only -- never the payload or credentials (section 11).
        run.error_message = str(exc)[:2000]
        _touch_freshness(
            session, provider, capability, scope, freshness_class,
            success=False, run_id=run.id, error=run.error_message,
        )
        session.flush()
        logger.warning("ingestion failed provider=%s capability=%s", provider.key, capability)
        raise
    else:
        run.status = "partial" if handle.partial else "success"
        run.finished_at = datetime.now(UTC)
        run.records_written = handle.records_written
        run.metrics = {
            "notes": handle.notes,
            "duration_seconds": round(
                (run.finished_at - started).total_seconds(), 3
            ),
        }
        _touch_freshness(
            session, provider, capability, scope, freshness_class,
            success=True, run_id=run.id, effective_at=handle.effective_at,
        )
        session.flush()


def _touch_freshness(
    session: Session,
    provider: SourceProvider,
    capability: str,
    scope: str,
    freshness_class: str | None,
    *,
    success: bool,
    run_id: str,
    effective_at: datetime | None = None,
    error: str | None = None,
) -> SourceFreshness:
    row = session.scalar(
        select(SourceFreshness).where(
            SourceFreshness.provider_id == provider.id,
            SourceFreshness.capability == capability,
            SourceFreshness.scope == scope,
        )
    )
    if row is None:
        row = SourceFreshness(
            provider_id=provider.id, capability=capability, scope=scope
        )
        session.add(row)
    row.freshness_class = freshness_class or provider.freshness_class
    row.last_attempt_at = datetime.now(UTC)
    row.last_ingestion_run_id = run_id
    if success:
        row.last_success_at = row.last_attempt_at
        row.last_effective_at = effective_at or row.last_attempt_at
        row.consecutive_failures = 0
        row.health = "healthy"
        row.last_error = None
    else:
        row.consecutive_failures += 1
        row.health = "failing" if row.consecutive_failures >= 3 else "degraded"
        row.last_error = error
    session.flush()
    return row


def collect_freshness(
    session: Session, capabilities: list[str] | None = None, scope: str | None = None
) -> list[FreshnessInput]:
    """Read current freshness state for the freshness evaluator."""
    stmt = select(SourceFreshness, SourceProvider).join(
        SourceProvider, SourceProvider.id == SourceFreshness.provider_id
    )
    if capabilities:
        stmt = stmt.where(SourceFreshness.capability.in_(capabilities))
    if scope:
        stmt = stmt.where(SourceFreshness.scope.in_([scope, "global"]))

    return [
        FreshnessInput(
            provider=provider.key,
            capability=row.capability,
            freshness_class=row.freshness_class,
            last_success_at=row.last_success_at,
            effective_at=row.last_effective_at,
            health=row.health,
            note=row.last_error,
        )
        for row, provider in session.execute(stmt).all()
    ]


def fetch_with_run(
    session: Session,
    provider: Provider,
    capability: str,
    *,
    scope: str = "global",
    trigger: str = "on_demand",
    required: bool = True,
    **params: object,
):
    """Fetch a capability inside a tracked run.

    When ``required`` is False a failure returns ``None`` instead of raising, so
    a missing weather feed degrades the answer rather than blocking it.
    """
    db_provider = upsert_provider(session, provider.spec)
    try:
        with ingestion_run(
            session, db_provider, capability, scope=scope, trigger=trigger,
            freshness_class=str(provider.spec.freshness_class),
        ) as handle:
            result = provider.fetch(capability, **params)
            handle.wrote(len(result.records))
            handle.partial = result.partial
            handle.notes = list(result.notes)
            handle.effective_at = result.effective_at
            return result
    except Exception:
        if required:
            raise
        logger.info(
            "optional source unavailable provider=%s capability=%s", provider.spec.key, capability
        )
        return None
