"""Draft board construction: value over replacement, tiers, cliffs, scarcity.

The board is the deterministic artifact `/prep draft` renders and `/draft
recommend` will consume. It answers the question that actually decides a pick:
not "who is best" but "what disappears if I wait".

Sorting is by value over replacement rather than raw projected points, because
raw points compare a quarterback to a running back as though they were
interchangeable. VOR compares each against the player you could have for free
at that position in *this* league, which is the only comparison that means
anything across positions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import erf, sqrt

from draftgpt.evaluation.league_shape import LeagueShape
from draftgpt.evaluation.replacement import (
    ReplacementLevel,
    compute_replacement_levels,
    detect_tier_cliffs,
)


@dataclass(frozen=True)
class BoardPlayer:
    player_id: str
    name: str
    position: str
    nfl_team: str | None
    projected_points: float
    floor_points: float | None = None
    ceiling_points: float | None = None
    adp: float | None = None
    adp_stdev: float | None = None
    adp_is_estimated: bool = False
    bye_week: int | None = None
    status: str = "active"
    is_drafted: bool = False


@dataclass
class BoardEntry:
    player: BoardPlayer
    value_over_replacement: float
    position_rank: int
    overall_rank: int
    tier: int
    points_to_next_tier: float | None = None
    is_last_of_tier: bool = False
    adp_delta: float | None = None  # positive = available later than value suggests

    def to_dict(self) -> dict:
        p = self.player
        return {
            "overall_rank": self.overall_rank,
            "player_id": p.player_id,
            "player": p.name,
            "position": p.position,
            "team": p.nfl_team,
            "position_rank": f"{p.position}{self.position_rank}",
            "projected_points": round(p.projected_points, 1),
            "value_over_replacement": round(self.value_over_replacement, 1),
            "tier": self.tier,
            "is_last_of_tier": self.is_last_of_tier,
            "points_to_next_tier": (
                round(self.points_to_next_tier, 1)
                if self.points_to_next_tier is not None
                else None
            ),
            "adp": p.adp,
            "adp_delta": round(self.adp_delta, 1) if self.adp_delta is not None else None,
            "bye_week": p.bye_week,
            "floor": round(p.floor_points, 1) if p.floor_points is not None else None,
            "ceiling": round(p.ceiling_points, 1) if p.ceiling_points is not None else None,
        }


@dataclass
class DraftBoard:
    entries: list[BoardEntry] = field(default_factory=list)
    replacement: dict[str, ReplacementLevel] = field(default_factory=dict)
    tier_breaks: dict[str, list[int]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def by_position(self, position: str) -> list[BoardEntry]:
        return [e for e in self.entries if e.player.position == position]

    def available(self) -> list[BoardEntry]:
        return [e for e in self.entries if not e.player.is_drafted]

    def find(self, player_id: str) -> BoardEntry | None:
        return next((e for e in self.entries if e.player.player_id == player_id), None)


def build_board(
    players: list[BoardPlayer],
    shape: LeagueShape,
    bench_depth_factor: float = 0.0,
    tier_sensitivity: float = 1.5,
) -> DraftBoard:
    """Rank the player pool by value over replacement for this league."""
    board = DraftBoard()
    if not players:
        board.warnings.append("no projections available -- board is empty")
        return board

    points_by_position: dict[str, list[float]] = {}
    for player in players:
        points_by_position.setdefault(player.position, []).append(player.projected_points)

    board.replacement = compute_replacement_levels(
        points_by_position,
        shape.starting_slots,
        shape.team_count,
        bench_depth_factor=bench_depth_factor,
    )

    # Tier boundaries are computed per position on the sorted point curve.
    tier_ranks: dict[str, list[int]] = {}
    for position, points in points_by_position.items():
        breaks = detect_tier_cliffs(points, position, sensitivity=tier_sensitivity)
        tier_ranks[position] = [b.after_rank for b in breaks]
    board.tier_breaks = tier_ranks

    entries: list[BoardEntry] = []
    for position in points_by_position:
        ordered = sorted(
            (p for p in players if p.position == position),
            key=lambda p: (-p.projected_points, p.name),
        )
        level = board.replacement.get(position)
        baseline = level.points if level else 0.0
        breaks = tier_ranks.get(position, [])

        for index, player in enumerate(ordered, start=1):
            tier = 1 + sum(1 for b in breaks if index > b)
            next_break = next((b for b in breaks if b >= index), None)
            points_to_next = None
            is_last = False
            if next_break is not None and next_break < len(ordered):
                is_last = index == next_break
                points_to_next = round(
                    player.projected_points - ordered[next_break].projected_points, 3
                )
            entries.append(
                BoardEntry(
                    player=player,
                    value_over_replacement=round(player.projected_points - baseline, 3),
                    position_rank=index,
                    overall_rank=0,  # assigned after the global sort
                    tier=tier,
                    points_to_next_tier=points_to_next,
                    is_last_of_tier=is_last,
                )
            )

    entries.sort(key=lambda e: (-e.value_over_replacement, e.player.name))
    for rank, entry in enumerate(entries, start=1):
        entry.overall_rank = rank
        if entry.player.adp is not None:
            # Positive means the market lets him fall past where value says he goes.
            entry.adp_delta = entry.player.adp - rank

    board.entries = entries
    missing_adp = sum(1 for e in entries if e.player.adp is None)
    if missing_adp:
        board.warnings.append(
            f"{missing_adp} of {len(entries)} players have no ADP; "
            "availability estimates are unavailable for them"
        )
    board.warnings.extend(_sanity_warnings(entries))
    return board


#: Positions that should never appear near the top of a value board. Their
#: week-to-week outcome is close to unpredictable and replacements are always
#: available, so real projections put their value over replacement in single
#: digits.
STREAMING_POSITIONS = frozenset({"K", "DST"})


def _sanity_warnings(entries: list[BoardEntry], top_n: int = 24) -> list[str]:
    """Catch projections that are obviously wrong before they reach advice.

    A kicker or defense inside the top of a value-over-replacement board is not
    a strategy insight -- it means the underlying projections are broken, most
    likely a stat-id mapping gap. Saying so is far more useful than confidently
    recommending a kicker in round two.
    """
    warnings: list[str] = []
    head = entries[:top_n]
    offenders = sorted(
        {e.player.position for e in head if e.player.position in STREAMING_POSITIONS}
    )
    if offenders:
        examples = ", ".join(
            f"{e.player.name} ({e.player.position}, rank {e.overall_rank})"
            for e in head
            if e.player.position in STREAMING_POSITIONS
        )
        warnings.append(
            f"SUSPECT PROJECTIONS: {'/'.join(offenders)} appear in the top {top_n} by value "
            f"over replacement -- {examples}. Streaming positions should never rank this "
            "high. This usually means the scoring stat-id map is wrong; run "
            "`draftgpt sources verify-espn`."
        )
    return warnings


# --------------------------------------------------------------------------
# Availability
# --------------------------------------------------------------------------
def survival_probability(
    adp: float, adp_stdev: float | None, pick_number: int
) -> float | None:
    """Probability a player is still available at ``pick_number``.

    Models draft position as normal around ADP and returns P(draft position >
    pick). Only as good as the dispersion estimate feeding it -- with ESPN as
    the source that number is approximated, not observed, so treat the output
    as a rough guide rather than a calibrated probability.
    """
    if adp is None or not adp_stdev or adp_stdev <= 0:
        return None
    z = (pick_number - adp) / adp_stdev
    cdf = 0.5 * (1.0 + erf(z / sqrt(2.0)))
    return round(max(0.0, min(1.0, 1.0 - cdf)), 3)


def picks_until_next_turn(
    current_pick: int, draft_position: int, team_count: int, is_snake: bool = True
) -> int | None:
    """How many picks pass before this drafter selects again."""
    if team_count <= 0 or draft_position <= 0:
        return None
    if not is_snake:
        return team_count

    round_index = (current_pick - 1) // team_count
    position_in_round = (current_pick - 1) % team_count + 1
    forward = round_index % 2 == 0
    my_slot = draft_position if forward else team_count - draft_position + 1
    if position_in_round > my_slot:
        return None

    # Distance to my next pick, accounting for the snake turn.
    if forward:
        picks_left_this_round = team_count - my_slot
        return picks_left_this_round * 2 + 1
    picks_left_this_round = team_count - my_slot
    return picks_left_this_round * 2 + 1


def scarcity_report(board: DraftBoard, shape: LeagueShape) -> list[dict]:
    """How thin each position is relative to what the league must start."""
    rows = []
    for position, demand in sorted(shape.demand.items(), key=lambda kv: -kv[1]):
        entries = [e for e in board.by_position(position) if not e.player.is_drafted]
        level = board.replacement.get(position)
        above_replacement = [e for e in entries if e.value_over_replacement > 0]
        rows.append(
            {
                "position": position,
                "starters_needed_league_wide": round(demand, 1),
                "available_above_replacement": len(above_replacement),
                "replacement_points": level.points if level else None,
                "replacement_rank": level.baseline_rank if level else None,
                "surplus": len(above_replacement) - int(round(demand)),
                "tier_breaks_at": board.tier_breaks.get(position, []),
            }
        )
    return rows


def roster_construction_plan(shape: LeagueShape, rounds: int) -> list[dict]:
    """A default positional plan derived from this league's own demand.

    Deliberately coarse: it allocates picks in proportion to where value is
    concentrated, then covers the mandatory single-slot positions at the end.
    It is a starting frame to argue with, not a script -- the board and ADP
    decide the actual pick.
    """
    per_team = {
        position: demand / shape.team_count for position, demand in shape.demand.items()
    }
    stream_positions = {"K", "DST"}
    core = {p: v for p, v in per_team.items() if p not in stream_positions}
    core_total = sum(core.values()) or 1.0

    reserved = len([p for p in stream_positions if per_team.get(p)])
    draftable = max(1, rounds - reserved)

    plan: list[dict] = []
    allocated: dict[str, int] = {}
    for position, weight in sorted(core.items(), key=lambda kv: -kv[1]):
        target = max(1, round(draftable * (weight / core_total)))
        allocated[position] = target

    # Trim or pad to fit the actual round count.
    while sum(allocated.values()) > draftable:
        widest = max(allocated, key=lambda p: allocated[p])
        allocated[widest] -= 1
    while sum(allocated.values()) < draftable:
        widest = max(core, key=lambda p: core[p])
        allocated[widest] += 1

    for position, count in sorted(allocated.items(), key=lambda kv: -kv[1]):
        plan.append(
            {
                "position": position,
                "target_count": count,
                "starters_required": round(per_team.get(position, 0), 2),
                "note": (
                    "priority: demand is highest here"
                    if count == max(allocated.values())
                    else "cover starters, then take upside"
                ),
            }
        )
    for position in sorted(stream_positions):
        if per_team.get(position):
            plan.append(
                {
                    "position": position,
                    "target_count": 1,
                    "starters_required": round(per_team[position], 2),
                    "note": "final rounds only -- streaming position",
                }
            )
    return plan
