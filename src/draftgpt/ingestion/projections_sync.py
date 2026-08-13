"""Projection ingestion.

Projections are stored as canonical stat lines, not as pre-scored points, so a
single ingested projection can be priced under any league's scoring rules -- and
re-priced correctly if the league changes its settings mid-season.

Historical projections are never overwritten: each ingest writes a new row
stamped with ``retrieved_at``, which is what makes a past recommendation
replayable exactly as it was made.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from draftgpt.database.models import Player, PlayerProviderId, Projection
from draftgpt.evaluation.scoring import ScoringRules, score_stat_line
from draftgpt.ingestion.identity import PlayerResolver
from draftgpt.ingestion.runner import fetch_with_run, upsert_provider
from draftgpt.providers.base import Provider

logger = logging.getLogger(__name__)


@dataclass
class ProjectionSyncReport:
    written: int = 0
    unresolved: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        text = f"{self.written} projections written"
        if self.unresolved:
            text += f", {len(self.unresolved)} unresolved players"
        return text


def sync_projections(
    session: Session,
    provider: Provider,
    season: int,
    week: int | None = None,
    scope: str = "week",
    rules: ScoringRules | None = None,
) -> ProjectionSyncReport:
    capability = {
        "week": "projections_weekly",
        "season": "projections_season",
        "ros": "projections_ros",
    }[scope]

    upsert_provider(session, provider.spec)
    report = ProjectionSyncReport()

    params = {"week": week} if scope == "week" else {}
    result = fetch_with_run(
        session,
        provider,
        capability,
        scope=f"season:{season}:week:{week}" if week else f"season:{season}",
        required=False,
        **params,
    )
    if result is None:
        report.notes.append(f"{provider.spec.key}:{capability} unavailable")
        return report
    report.notes.extend(result.notes)

    db_provider = upsert_provider(session, provider.spec)
    resolver = PlayerResolver(session)
    id_map = {
        m.external_id: m.player_id
        for m in session.scalars(
            select(PlayerProviderId).where(
                PlayerProviderId.provider_key == provider.spec.key
            )
        )
    }
    retrieved = result.retrieved_at or datetime.now(UTC)

    for record in result.records:
        player_id = _resolve(session, resolver, provider.spec.key, record, id_map)
        if player_id is None:
            report.unresolved.append(record.get("name") or str(record.get("player_external_id")))
            continue

        stat_line = record.get("stat_line") or {}
        points = record.get("projected_points")
        if points is None and rules is not None and stat_line:
            points = score_stat_line(stat_line, rules)

        session.add(
            Projection(
                player_id=player_id,
                season=record.get("season") or season,
                week=record.get("week") if scope == "week" else None,
                scope=scope,
                stat_line=stat_line,
                projected_points=points,
                floor_points=record.get("floor_points"),
                ceiling_points=record.get("ceiling_points"),
                stdev_points=record.get("stdev_points"),
                scoring_ruleset=rules.ruleset_id if rules else None,
                provider_id=db_provider.id,
                retrieved_at=retrieved,
                effective_at=result.effective_at or retrieved,
            )
        )
        report.written += 1

    session.flush()
    logger.info("projection sync: %s", report.summary())
    return report


def _resolve(
    session: Session,
    resolver: PlayerResolver,
    provider_key: str,
    record: dict,
    id_map: dict[str, str],
) -> str | None:
    external_id = record.get("player_external_id")
    if external_id and str(external_id) in id_map:
        return id_map[str(external_id)]

    outcome = resolver.resolve_provider_player(
        provider_key=provider_key,
        external_id=str(external_id) if external_id else "",
        name=record.get("name"),
        position=record.get("position"),
        nfl_team=record.get("team"),
    )
    if not outcome.resolved:
        return None
    if external_id and outcome.method != "provider_id":
        resolver.link(
            player_id=outcome.player_id,  # type: ignore[arg-type]
            provider_key=provider_key,
            external_id=str(external_id),
            external_name=record.get("name"),
            method=outcome.method or "fuzzy",
            confidence=outcome.confidence,
        )
        id_map[str(external_id)] = outcome.player_id  # type: ignore[index]
    return outcome.player_id


def latest_projections(
    session: Session,
    player_ids: list[str],
    season: int,
    week: int | None,
    scope: str = "week",
    as_of: datetime | None = None,
) -> dict[str, Projection]:
    """Most recent projection per player at or before ``as_of``.

    The ``as_of`` bound is what makes backtesting honest: replaying a week-3
    decision must not see a projection retrieved in week 4.
    """
    if not player_ids:
        return {}
    stmt = select(Projection).where(
        Projection.player_id.in_(player_ids),
        Projection.season == season,
        Projection.scope == scope,
    )
    if scope == "week" and week is not None:
        stmt = stmt.where(Projection.week == week)
    if as_of is not None:
        stmt = stmt.where(Projection.retrieved_at <= as_of)

    best: dict[str, Projection] = {}
    for projection in session.scalars(stmt.order_by(Projection.retrieved_at)):
        best[projection.player_id] = projection  # later rows win
    return best


def price_projection(
    projection: Projection, rules: ScoringRules
) -> tuple[float, float | None, float | None]:
    """Return (median, floor, ceiling) points under a league's scoring rules."""
    if projection.stat_line:
        median = score_stat_line(projection.stat_line, rules)
    else:
        median = projection.projected_points or 0.0
    return median, projection.floor_points, projection.ceiling_points


def players_by_id(session: Session, player_ids: list[str]) -> dict[str, Player]:
    if not player_ids:
        return {}
    return {
        p.id: p for p in session.scalars(select(Player).where(Player.id.in_(player_ids)))
    }
