"""Value over replacement and positional tier/cliff detection.

Replacement level is league-specific: it is the quality of player you could get
for free given how many of that position the league *starts*, not a league-
agnostic constant. A 10-team league with one RB slot has a far shallower
replacement bar than a 14-team league with two plus a flex.
"""

from __future__ import annotations

from dataclasses import dataclass

from draftgpt.evaluation.lineup import SlotSpec

FLEX_POSITIONS = ("RB", "WR", "TE")


@dataclass(frozen=True)
class ReplacementLevel:
    position: str
    baseline_rank: int
    points: float
    sample_size: int


def starters_by_position(slots: list[SlotSpec], team_count: int) -> dict[str, float]:
    """League-wide demand for each position, counting flex slots fractionally.

    A flex slot is split across its eligible positions by an empirical share
    rather than evenly: RB/WR absorb nearly all flex usage in practice, TE
    rarely. Weights are configurable via ``FLEX_SHARE``.
    """
    demand: dict[str, float] = {}
    for slot in slots:
        eligible = slot.eligible_positions
        if not eligible:
            continue
        if len(eligible) == 1:
            demand[eligible[0]] = demand.get(eligible[0], 0.0) + team_count
            continue
        shares = _flex_shares(eligible)
        for position, share in shares.items():
            demand[position] = demand.get(position, 0.0) + team_count * share
    return demand


#: Share of a multi-position slot that each position tends to occupy.
FLEX_SHARE: dict[str, float] = {"RB": 0.42, "WR": 0.48, "TE": 0.10, "QB": 0.85}


def _flex_shares(eligible: tuple[str, ...]) -> dict[str, float]:
    if "QB" in eligible:  # superflex: QBs dominate the slot
        remainder = 1.0 - FLEX_SHARE["QB"]
        others = [p for p in eligible if p != "QB"]
        return {"QB": FLEX_SHARE["QB"]} | {
            p: remainder * (FLEX_SHARE.get(p, 0.1) / sum(FLEX_SHARE.get(o, 0.1) for o in others))
            for p in others
        }
    weights = {p: FLEX_SHARE.get(p, 1.0 / len(eligible)) for p in eligible}
    total = sum(weights.values()) or 1.0
    return {p: w / total for p, w in weights.items()}


def compute_replacement_levels(
    points_by_position: dict[str, list[float]],
    slots: list[SlotSpec],
    team_count: int,
    bench_depth_factor: float = 0.0,
) -> dict[str, ReplacementLevel]:
    """Replacement points per position.

    ``bench_depth_factor`` pushes the baseline deeper to account for teams
    carrying backups (useful in-season for waiver math, near zero for draft-day
    starter value).
    """
    demand = starters_by_position(slots, team_count)
    levels: dict[str, ReplacementLevel] = {}
    for position, points in points_by_position.items():
        ordered = sorted(points, reverse=True)
        if not ordered:
            continue
        starters = demand.get(position, 0.0)
        baseline_rank = max(1, int(round(starters * (1.0 + bench_depth_factor))))
        index = min(baseline_rank, len(ordered)) - 1
        levels[position] = ReplacementLevel(
            position=position,
            baseline_rank=baseline_rank,
            points=round(ordered[index], 3),
            sample_size=len(ordered),
        )
    return levels


def value_over_replacement(
    points: float, position: str, levels: dict[str, ReplacementLevel]
) -> float:
    level = levels.get(position)
    if level is None:
        return round(points, 3)
    return round(points - level.points, 3)


@dataclass(frozen=True)
class TierBreak:
    """A gap in the sorted point curve for a position."""

    position: str
    after_rank: int
    gap: float
    next_player_points: float


def detect_tier_cliffs(
    ranked_points: list[float], position: str, sensitivity: float = 1.5
) -> list[TierBreak]:
    """Find point gaps materially larger than the local average gap.

    Used by `/draft recommend` to answer "is this the last player of his tier?",
    which is the question that actually drives a reach or a wait.
    """
    if len(ranked_points) < 3:
        return []
    ordered = sorted(ranked_points, reverse=True)
    gaps = [ordered[i] - ordered[i + 1] for i in range(len(ordered) - 1)]
    mean_gap = sum(gaps) / len(gaps)
    if mean_gap <= 0:
        return []
    breaks = [
        TierBreak(
            position=position,
            after_rank=i + 1,
            gap=round(gap, 3),
            next_player_points=round(ordered[i + 1], 3),
        )
        for i, gap in enumerate(gaps)
        if gap >= mean_gap * sensitivity
    ]
    return breaks
