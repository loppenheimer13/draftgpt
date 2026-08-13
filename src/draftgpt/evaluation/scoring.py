"""League-specific fantasy scoring.

Deterministic and pure: a stat line plus a scoring ruleset produces points. No
database access, no provider knowledge, no LLM. This is the function every
projection and every actual result is priced through, so a league's PPR setting
or its 40-yard-TD bonus changes every downstream recommendation automatically.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

StatLine = dict[str, float]


@dataclass(frozen=True)
class Bonus:
    """A threshold bonus, e.g. 3 points for a 300-yard passing game.

    ``repeatable`` awards the bonus once per full ``threshold`` reached (some
    leagues score every 100 rushing yards); otherwise it is awarded at most once.
    """

    stat: str
    threshold: float
    points: float
    repeatable: bool = False

    def evaluate(self, stats: StatLine) -> float:
        value = stats.get(self.stat, 0.0)
        if self.threshold <= 0:
            return 0.0
        if value < self.threshold:
            return 0.0
        if self.repeatable:
            return self.points * (int(value // self.threshold))
        return self.points


@dataclass(frozen=True)
class PointsAllowedTier:
    """Team-defense points-allowed bracket: [min, max] inclusive -> points."""

    min_points: float
    max_points: float
    points: float

    def matches(self, value: float) -> bool:
        return self.min_points <= value <= self.max_points


@dataclass(frozen=True)
class ScoringRules:
    """Normalized scoring configuration for one league ruleset version."""

    per_stat: dict[str, float] = field(default_factory=dict)
    bonuses: tuple[Bonus, ...] = ()
    points_allowed_tiers: tuple[PointsAllowedTier, ...] = ()
    yards_allowed_tiers: tuple[PointsAllowedTier, ...] = ()
    ruleset_id: str | None = None

    @classmethod
    def from_config(cls, config: dict[str, Any], ruleset_id: str | None = None) -> ScoringRules:
        """Build from the JSON stored on ``league_rules.scoring``."""
        per_stat = {
            key: float(value)
            for key, value in config.items()
            if key not in {"bonuses", "points_allowed_tiers", "yards_allowed_tiers"}
            and isinstance(value, int | float)
        }
        bonuses = tuple(
            Bonus(
                stat=str(b["stat"]),
                threshold=float(b["threshold"]),
                points=float(b["points"]),
                repeatable=bool(b.get("repeatable", False)),
            )
            for b in config.get("bonuses", [])
        )
        pa = tuple(
            PointsAllowedTier(float(t["min"]), float(t["max"]), float(t["points"]))
            for t in config.get("points_allowed_tiers", [])
        )
        ya = tuple(
            PointsAllowedTier(float(t["min"]), float(t["max"]), float(t["points"]))
            for t in config.get("yards_allowed_tiers", [])
        )
        return cls(
            per_stat=per_stat,
            bonuses=bonuses,
            points_allowed_tiers=pa,
            yards_allowed_tiers=ya,
            ruleset_id=ruleset_id,
        )

    @property
    def is_ppr(self) -> bool:
        return self.per_stat.get("rec", 0.0) >= 1.0

    @property
    def reception_value(self) -> float:
        return self.per_stat.get("rec", 0.0)


def score_stat_line(stats: StatLine, rules: ScoringRules) -> float:
    """Total fantasy points for a stat line under one league's rules."""
    total = 0.0
    for stat, per_unit in rules.per_stat.items():
        # Tiered defensive stats are handled by their bracket tables, not linearly.
        if stat in {"def_pts_allowed", "def_yds_allowed"} and (
            rules.points_allowed_tiers or rules.yards_allowed_tiers
        ):
            continue
        value = stats.get(stat)
        if value:
            total += float(value) * per_unit

    for bonus in rules.bonuses:
        total += bonus.evaluate(stats)

    if rules.points_allowed_tiers and "def_pts_allowed" in stats:
        total += _tier_points(stats["def_pts_allowed"], rules.points_allowed_tiers)
    if rules.yards_allowed_tiers and "def_yds_allowed" in stats:
        total += _tier_points(stats["def_yds_allowed"], rules.yards_allowed_tiers)

    return round(total, 4)


def _tier_points(value: float, tiers: tuple[PointsAllowedTier, ...]) -> float:
    for tier in tiers:
        if tier.matches(value):
            return tier.points
    return 0.0


def score_breakdown(stats: StatLine, rules: ScoringRules) -> dict[str, float]:
    """Per-stat point contributions. Used as recommendation evidence so a
    surprising projection can be traced to the stat driving it."""
    breakdown: dict[str, float] = {}
    for stat, per_unit in rules.per_stat.items():
        if stat in {"def_pts_allowed", "def_yds_allowed"} and (
            rules.points_allowed_tiers or rules.yards_allowed_tiers
        ):
            continue
        value = stats.get(stat)
        if value:
            breakdown[stat] = round(float(value) * per_unit, 4)
    for bonus in rules.bonuses:
        earned = bonus.evaluate(stats)
        if earned:
            breakdown[f"bonus:{bonus.stat}>={bonus.threshold:g}"] = round(earned, 4)
    if rules.points_allowed_tiers and "def_pts_allowed" in stats:
        earned = _tier_points(stats["def_pts_allowed"], rules.points_allowed_tiers)
        if earned:
            breakdown["def_pts_allowed_tier"] = round(earned, 4)
    if rules.yards_allowed_tiers and "def_yds_allowed" in stats:
        earned = _tier_points(stats["def_yds_allowed"], rules.yards_allowed_tiers)
        if earned:
            breakdown["def_yds_allowed_tier"] = round(earned, 4)
    return breakdown
