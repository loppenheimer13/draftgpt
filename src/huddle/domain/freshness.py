"""Freshness policy for the daily show.

Freshness is not one number. A team's roster may be a week old and still be
right; last night's score cannot be. So the maximum acceptable age is a
function of (freshness class, season phase), and the show degrades rather than
blocks when a non-critical source is behind.

Degrading matters more here than in most systems, because the audience is a
child. The show never reads an error message. It either has the score, or it
says the score is not in yet and moves on to something it does know.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal

from huddle.domain.enums import Capability, FreshnessClass, SeasonPhase

HOUR = 3600.0
DAY = 24 * HOUR

FreshnessStatus = Literal["fresh", "degraded", "stale"]

#: Max acceptable age in seconds for (freshness class, season phase).
MAX_AGE: dict[tuple[str, str], float] = {
    (FreshnessClass.STATIC, "*"): 30 * DAY,
    (FreshnessClass.SLOW, SeasonPhase.OFFSEASON): 30 * DAY,
    (FreshnessClass.SLOW, "*"): 14 * DAY,
    (FreshnessClass.DAILY, SeasonPhase.OFFSEASON): 14 * DAY,
    (FreshnessClass.DAILY, SeasonPhase.REGULAR_SEASON): 36 * HOUR,
    (FreshnessClass.DAILY, SeasonPhase.GAME_DAY): 18 * HOUR,
    (FreshnessClass.DAILY, SeasonPhase.POSTSEASON): 24 * HOUR,
    (FreshnessClass.DAILY, "*"): 3 * DAY,
    (FreshnessClass.RAPID, SeasonPhase.OFFSEASON): 3 * DAY,
    (FreshnessClass.RAPID, SeasonPhase.REGULAR_SEASON): 18 * HOUR,
    (FreshnessClass.RAPID, SeasonPhase.GAME_DAY): 6 * HOUR,
    (FreshnessClass.RAPID, SeasonPhase.POSTSEASON): 12 * HOUR,
    (FreshnessClass.RAPID, "*"): 2 * DAY,
    (FreshnessClass.LIVE, SeasonPhase.GAME_DAY): 30 * 60,
    (FreshnessClass.LIVE, "*"): 2 * HOUR,
}

#: Without these the show has nothing to talk about, so a stale one is
#: reported as stale outright rather than merely lowering confidence.
CRITICAL_CAPABILITIES: dict[str, set[str]] = {
    "show": {Capability.LEAGUE_TEAMS},
    "your_teams": {Capability.TEAM_DETAIL, Capability.SCOREBOARD},
    "today_in_sports": {Capability.SCOREBOARD},
    "birthday_club": {Capability.TEAM_ROSTER},
    "weekend_edition": {Capability.SCOREBOARD},
}

#: Confidence multiplier applied per degraded/stale source.
DEGRADED_PENALTY = 0.90
STALE_PENALTY = 0.70


def max_age_for(freshness_class: str, phase: str) -> float:
    return MAX_AGE.get((freshness_class, phase)) or MAX_AGE.get((freshness_class, "*"), DAY)


def infer_season_phase(now: datetime, *, has_games_today: bool = False) -> SeasonPhase:
    """Derive the phase from the calendar, sharpened by whether games are on.

    With fourteen leagues spanning the whole year there is no true offseason,
    so this is really asking "is anything the family follows playing today" --
    which is the only distinction the freshness limits care about.
    """
    if has_games_today:
        return SeasonPhase.GAME_DAY
    if now.month in (1, 2, 6):
        return SeasonPhase.POSTSEASON
    if now.month == 7:
        return SeasonPhase.OFFSEASON
    return SeasonPhase.REGULAR_SEASON


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


@dataclass(frozen=True)
class SourceFreshnessReport:
    provider: str
    capability: str
    freshness_class: str
    status: FreshnessStatus
    last_success_at: datetime | None = None
    effective_at: datetime | None = None
    age_seconds: float | None = None
    max_age_seconds: float | None = None
    health: str = "unknown"
    note: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "capability": self.capability,
            "status": self.status,
            "age_seconds": self.age_seconds,
            "max_age_seconds": self.max_age_seconds,
            "health": self.health,
            "note": self.note,
        }


@dataclass(frozen=True)
class FreshnessBlock:
    status: FreshnessStatus
    warnings: list[str] = field(default_factory=list)
    sources: list[SourceFreshnessReport] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "warnings": list(self.warnings),
            "sources": [s.as_dict() for s in self.sources],
        }


def evaluate_freshness(
    inputs: list[FreshnessInput],
    section: str = "show",
    phase: str = SeasonPhase.REGULAR_SEASON,
    now: datetime | None = None,
) -> tuple[FreshnessBlock, float]:
    """Return the freshness block and a confidence multiplier in (0, 1]."""
    now = now or datetime.now(UTC)
    critical = CRITICAL_CAPABILITIES.get(section, set())
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

        if item.health == "failing" and status == "fresh":
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
        worst = _worse(worst, "stale" if (is_critical and status == "stale") else "degraded")

    return FreshnessBlock(status=worst, warnings=warnings, sources=reports), max(multiplier, 0.2)


def next_publish_time(
    phase: str,
    now: datetime | None = None,
    *,
    kickoff_at: datetime | None = None,
) -> datetime:
    """When the next episode is worth building."""
    now = now or datetime.now(UTC)
    if kickoff_at is not None:
        lead = kickoff_at - timedelta(minutes=90)
        if lead > now:
            return lead
    intervals = {
        SeasonPhase.OFFSEASON: timedelta(hours=24),
        SeasonPhase.REGULAR_SEASON: timedelta(hours=24),
        SeasonPhase.GAME_DAY: timedelta(hours=12),
        SeasonPhase.POSTSEASON: timedelta(hours=24),
    }
    return now + intervals.get(SeasonPhase(phase), timedelta(hours=24))


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
