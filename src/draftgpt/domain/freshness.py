"""Decision-dependent freshness policy (brief section 4).

Freshness is not one number. A preseason ranking may be a week old in May; an
inactive designation cannot be a minute stale at kickoff. The max acceptable age
is therefore a function of (freshness class, season phase), and a command
degrades rather than blocks when a non-critical source is behind.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from draftgpt.domain.contract import (
    FreshnessBlock,
    FreshnessStatus,
    SourceFreshnessReport,
)
from draftgpt.domain.enums import Capability, FreshnessClass, SeasonPhase

HOUR = 3600.0
DAY = 24 * HOUR

#: Max acceptable age in seconds for (freshness class, season phase).
MAX_AGE: dict[tuple[str, str], float] = {
    (FreshnessClass.STATIC, "*"): 30 * DAY,
    (FreshnessClass.SLOW, SeasonPhase.EARLY_PRESEASON): 10 * DAY,
    (FreshnessClass.SLOW, SeasonPhase.PRE_DRAFT): 2 * DAY,
    (FreshnessClass.SLOW, SeasonPhase.DRAFT_DAY): 12 * HOUR,
    (FreshnessClass.SLOW, "*"): 7 * DAY,
    (FreshnessClass.DAILY, SeasonPhase.EARLY_PRESEASON): 7 * DAY,
    (FreshnessClass.DAILY, SeasonPhase.PRE_DRAFT): 36 * HOUR,
    (FreshnessClass.DAILY, SeasonPhase.DRAFT_DAY): 8 * HOUR,
    (FreshnessClass.DAILY, SeasonPhase.REGULAR_SEASON): 24 * HOUR,
    (FreshnessClass.DAILY, SeasonPhase.GAME_DAY): 12 * HOUR,
    (FreshnessClass.DAILY, "*"): 3 * DAY,
    (FreshnessClass.RAPID, SeasonPhase.REGULAR_SEASON): 12 * HOUR,
    (FreshnessClass.RAPID, SeasonPhase.GAME_DAY): 2 * HOUR,
    (FreshnessClass.RAPID, SeasonPhase.DRAFT_DAY): 3 * HOUR,
    (FreshnessClass.RAPID, "*"): 2 * DAY,
    (FreshnessClass.LIVE, SeasonPhase.GAME_DAY): 15 * 60,
    (FreshnessClass.LIVE, SeasonPhase.DRAFT_DAY): 2 * 60,
    (FreshnessClass.LIVE, "*"): HOUR,
}

#: Capabilities a command cannot produce a trustworthy answer without. Missing
#: critical sources force "stale"; missing others only degrade confidence.
CRITICAL_CAPABILITIES: dict[str, set[str]] = {
    "roster": {Capability.LEAGUE_ROSTERS, Capability.LEAGUE_SETTINGS},
    "draft": {Capability.LEAGUE_SETTINGS, Capability.LEAGUE_DRAFT},
    "waiver": {Capability.LEAGUE_ROSTERS, Capability.LEAGUE_FREE_AGENTS},
    "trade": {Capability.LEAGUE_ROSTERS, Capability.LEAGUE_SETTINGS},
    "pulse": {Capability.LEAGUE_ROSTERS},
}

#: Confidence multiplier applied per degraded/stale source.
DEGRADED_PENALTY = 0.90
STALE_PENALTY = 0.70


def max_age_for(freshness_class: str, phase: str) -> float:
    return MAX_AGE.get((freshness_class, phase)) or MAX_AGE.get((freshness_class, "*"), DAY)


def infer_season_phase(
    now: datetime,
    *,
    draft_at: datetime | None = None,
    league_status: str | None = None,
    has_games_today: bool = False,
) -> SeasonPhase:
    """Derive the phase from league state rather than the calendar alone, so a
    late-August dynasty startup and a September redraft both behave correctly."""
    if draft_at is not None:
        delta = draft_at - now
        if timedelta(0) <= delta <= timedelta(hours=12):
            return SeasonPhase.DRAFT_DAY
        if timedelta(0) < delta <= timedelta(days=21):
            return SeasonPhase.PRE_DRAFT
    if league_status == "drafting":
        return SeasonPhase.DRAFT_DAY
    if league_status == "in_season":
        return SeasonPhase.GAME_DAY if has_games_today else SeasonPhase.REGULAR_SEASON
    if league_status == "complete":
        return SeasonPhase.OFFSEASON
    if league_status == "pre_draft":
        return SeasonPhase.EARLY_PRESEASON
    return SeasonPhase.REGULAR_SEASON if 9 <= now.month <= 12 else SeasonPhase.EARLY_PRESEASON


@dataclass(frozen=True)
class FreshnessInput:
    """One source's observed state, as read from ``source_freshness``."""

    provider: str
    capability: str
    freshness_class: str
    last_success_at: datetime | None
    effective_at: datetime | None = None
    health: str = "unknown"
    note: str | None = None


def evaluate_freshness(
    inputs: list[FreshnessInput],
    command: str,
    phase: str,
    now: datetime | None = None,
) -> tuple[FreshnessBlock, float]:
    """Return the freshness block and a confidence multiplier in (0, 1].

    A command never blocks on a non-critical stale source: it reports the
    warning, reduces confidence, and still answers (brief section 4).
    """
    now = now or datetime.now(UTC)
    critical = CRITICAL_CAPABILITIES.get(command, set())
    reports: list[SourceFreshnessReport] = []
    warnings: list[str] = []
    multiplier = 1.0
    worst: FreshnessStatus = "fresh"

    for item in inputs:
        max_age = max_age_for(item.freshness_class, phase)
        reference = item.effective_at or item.last_success_at
        age = (now - _aware(reference)).total_seconds() if reference else None

        if age is None:
            status: FreshnessStatus = "stale"
            note = "never successfully ingested"
        elif age > max_age * 2:
            status, note = "stale", f"{_humanize(age)} old (limit {_humanize(max_age)})"
        elif age > max_age:
            status, note = "degraded", f"{_humanize(age)} old (limit {_humanize(max_age)})"
        else:
            status, note = "fresh", None

        if item.health in {"failing"} and status == "fresh":
            status, note = "degraded", "provider reporting failures"

        reports.append(
            SourceFreshnessReport(
                provider=item.provider,
                capability=item.capability,
                freshness_class=item.freshness_class,
                last_success_at=item.last_success_at,
                effective_at=item.effective_at,
                age_seconds=round(age, 1) if age is not None else None,
                max_age_seconds=max_age,
                status=status,
                health=item.health,
                note=item.note or note,
            )
        )

        if status == "fresh":
            continue

        is_critical = item.capability in critical
        label = "CRITICAL " if is_critical else ""
        warnings.append(f"{label}{item.provider}:{item.capability} is {status} -- {note}")
        multiplier *= STALE_PENALTY if status == "stale" else DEGRADED_PENALTY
        if is_critical or status == "stale":
            worst = "stale" if (is_critical and status == "stale") else _worse(worst, "degraded")
        else:
            worst = _worse(worst, "degraded")

    return FreshnessBlock(status=worst, warnings=warnings, sources=reports), max(multiplier, 0.2)


def next_check_time(
    phase: str,
    now: datetime | None = None,
    *,
    kickoff_at: datetime | None = None,
) -> datetime:
    """When the owner should run this command again.

    Anchored to the next real decision boundary when one exists (kickoff), not
    to a fixed interval -- "check again in 6 hours" is useless 20 minutes before
    a 1pm slate.
    """
    now = now or datetime.now(UTC)
    if kickoff_at is not None:
        lead = kickoff_at - timedelta(minutes=90)
        if lead > now:
            return lead
    intervals = {
        SeasonPhase.EARLY_PRESEASON: timedelta(days=7),
        SeasonPhase.PRE_DRAFT: timedelta(days=1),
        SeasonPhase.DRAFT_DAY: timedelta(minutes=15),
        SeasonPhase.REGULAR_SEASON: timedelta(hours=12),
        SeasonPhase.GAME_DAY: timedelta(hours=2),
        SeasonPhase.OFFSEASON: timedelta(days=30),
    }
    return now + intervals.get(SeasonPhase(phase), timedelta(hours=12))


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _worse(a: FreshnessStatus, b: FreshnessStatus) -> FreshnessStatus:
    order = {"fresh": 0, "degraded": 1, "stale": 2}
    return a if order[a] >= order[b] else b


def _humanize(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    if seconds < 2 * DAY:
        return f"{seconds / HOUR:.1f}h"
    return f"{seconds / DAY:.1f}d"
