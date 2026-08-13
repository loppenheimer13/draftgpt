"""Lineup eligibility and optimization.

The optimal lineup is a maximum-weight assignment of players to starting slots.
Slot eligibility forms a transversal matroid, so processing players in
descending weight and admitting each one whenever an augmenting path exists
yields the provably optimal lineup -- greedy on a matroid, not a heuristic. That
matters: naive "best player into the most restrictive open slot" mis-solves the
common case where a WR-only player must displace a WR sitting in FLEX.

No third-party solver is required; rosters are small and the matching is exact.
"""

from __future__ import annotations

from dataclasses import dataclass, field

Objective = str  # "floor" | "balanced" | "ceiling"

#: Weight applied to (floor, median, ceiling) per risk objective.
OBJECTIVE_WEIGHTS: dict[Objective, tuple[float, float, float]] = {
    "floor": (0.55, 0.45, 0.0),
    "balanced": (0.0, 1.0, 0.0),
    "ceiling": (0.0, 0.45, 0.55),
}


@dataclass(frozen=True)
class SlotSpec:
    """One startable position. Multi-count slots are expanded to one spec each."""

    slot_id: str
    slot: str
    ordinal: int
    eligible_positions: tuple[str, ...]
    display_order: int = 0

    def accepts(self, positions: tuple[str, ...]) -> bool:
        return any(p in self.eligible_positions for p in positions)


@dataclass(frozen=True)
class PlayerOption:
    """A rosterable player with its priced projection for the target week."""

    player_id: str
    name: str
    positions: tuple[str, ...]
    median: float
    floor: float | None = None
    ceiling: float | None = None
    nfl_team: str | None = None
    opponent: str | None = None
    is_locked: bool = False
    locked_slot_id: str | None = None
    status: str = "active"
    on_bye: bool = False
    projection_missing: bool = False

    def weight(self, objective: Objective) -> float:
        w_floor, w_median, w_ceiling = OBJECTIVE_WEIGHTS.get(
            objective, OBJECTIVE_WEIGHTS["balanced"]
        )
        floor = self.floor if self.floor is not None else self.median * 0.6
        ceiling = self.ceiling if self.ceiling is not None else self.median * 1.5
        return w_floor * floor + w_median * self.median + w_ceiling * ceiling


@dataclass
class LineupSolution:
    assignments: dict[str, str] = field(default_factory=dict)  # slot_id -> player_id
    bench: list[str] = field(default_factory=list)
    unfilled_slots: list[str] = field(default_factory=list)
    objective: Objective = "balanced"

    total_median: float = 0.0
    total_floor: float = 0.0
    total_ceiling: float = 0.0

    def starters(self) -> set[str]:
        return set(self.assignments.values())


def optimize_lineup(
    players: list[PlayerOption],
    slots: list[SlotSpec],
    objective: Objective = "balanced",
) -> LineupSolution:
    """Return the maximum-weight legal starting lineup.

    Players marked ``is_locked`` (their NFL game has kicked off) are pinned to
    their current slot before optimization, which is what makes late-swap advice
    honest rather than a lineup the platform would reject.
    """
    starting = [s for s in slots if s.eligible_positions]
    by_id = {p.player_id: p for p in players}

    pinned: dict[str, str] = {}
    for player in players:
        if (
            player.is_locked
            and player.locked_slot_id
            and any(s.slot_id == player.locked_slot_id for s in starting)
        ):
            pinned[player.locked_slot_id] = player.player_id

    open_slots = [s for s in starting if s.slot_id not in pinned]
    movable = [
        p for p in players if p.player_id not in set(pinned.values()) and not _is_ineligible(p)
    ]

    # Deterministic ordering: weight desc, then name, then id. Ties must resolve
    # identically across runs or a replayed snapshot could produce a different lineup.
    movable.sort(key=lambda p: (-p.weight(objective), p.name, p.player_id))

    slot_to_player: dict[str, str] = {}
    for player in movable:
        _try_assign(player, open_slots, slot_to_player, by_id, objective, set())

    assignments = {**pinned, **slot_to_player}
    assigned_players = set(assignments.values())
    bench = [p.player_id for p in players if p.player_id not in assigned_players]
    unfilled = [s.slot_id for s in starting if s.slot_id not in assignments]

    solution = LineupSolution(
        assignments=assignments,
        bench=bench,
        unfilled_slots=unfilled,
        objective=objective,
    )
    for player_id in assigned_players:
        option = by_id[player_id]
        solution.total_median += option.median
        solution.total_floor += option.floor if option.floor is not None else option.median * 0.6
        solution.total_ceiling += (
            option.ceiling if option.ceiling is not None else option.median * 1.5
        )
    solution.total_median = round(solution.total_median, 2)
    solution.total_floor = round(solution.total_floor, 2)
    solution.total_ceiling = round(solution.total_ceiling, 2)
    return solution


def _is_ineligible(player: PlayerOption) -> bool:
    """Players who cannot legally or sensibly start. On-bye and OUT players are
    excluded outright rather than scored at zero, so they never displace a
    startable player through a tie."""
    return player.on_bye or player.status.lower() in {"out", "o", "ir", "suspended", "doubtful_out"}


def _try_assign(
    player: PlayerOption,
    slots: list[SlotSpec],
    slot_to_player: dict[str, str],
    by_id: dict[str, PlayerOption],
    objective: Objective,
    visited: set[str],
) -> bool:
    """Kuhn's augmenting path: place ``player``, displacing occupants that can
    themselves move elsewhere."""
    for slot in slots:
        if slot.slot_id in visited or not slot.accepts(player.positions):
            continue
        visited.add(slot.slot_id)
        occupant_id = slot_to_player.get(slot.slot_id)
        if occupant_id is None:
            slot_to_player[slot.slot_id] = player.player_id
            return True
        occupant = by_id[occupant_id]
        if _try_assign(occupant, slots, slot_to_player, by_id, objective, visited):
            slot_to_player[slot.slot_id] = player.player_id
            return True
    return False


@dataclass(frozen=True)
class LineupChange:
    """A change the owner actually has to make.

    ``kind`` is one of:
      * ``swap``  -- bench one player, start another
      * ``start`` -- fill a slot that was empty
      * ``bench`` -- sit a player with no replacement (slot left empty)
      * ``move``  -- same player, different slot type (e.g. WR -> FLEX)
    """

    kind: str
    slot: str
    slot_id: str | None = None
    player_in: str | None = None
    player_out: str | None = None
    from_slot: str | None = None


def lineup_delta(
    current: dict[str, str],
    proposed: dict[str, str],
    slots: list[SlotSpec] | None = None,
) -> list[LineupChange]:
    """Meaningful differences between the configured and recommended lineups.

    Compared by *player*, not by slot id. Two identical RB slots are
    interchangeable, so a solver that happens to assign them in the opposite
    order has changed nothing -- reporting that as two swaps would be noise that
    makes a no-op recommendation look like work.
    """
    slot_name = {s.slot_id: s.slot for s in (slots or [])}

    current_slot_of = {pid: sid for sid, pid in current.items()}
    proposed_slot_of = {pid: sid for sid, pid in proposed.items()}
    started_before, starting_now = set(current_slot_of), set(proposed_slot_of)

    promoted = sorted(starting_now - started_before)
    benched = sorted(started_before - starting_now)

    changes: list[LineupChange] = []

    # Pair each promotion with a demotion into a single swap instruction.
    for player_in, player_out in zip(promoted, benched, strict=False):
        slot_id = proposed_slot_of[player_in]
        changes.append(
            LineupChange(
                kind="swap",
                slot=slot_name.get(slot_id, slot_id),
                slot_id=slot_id,
                player_in=player_in,
                player_out=player_out,
            )
        )

    for player_in in promoted[len(benched):]:
        slot_id = proposed_slot_of[player_in]
        changes.append(
            LineupChange(
                kind="start",
                slot=slot_name.get(slot_id, slot_id),
                slot_id=slot_id,
                player_in=player_in,
            )
        )

    for player_out in benched[len(promoted):]:
        slot_id = current_slot_of[player_out]
        changes.append(
            LineupChange(
                kind="bench",
                slot=slot_name.get(slot_id, slot_id),
                slot_id=slot_id,
                player_out=player_out,
            )
        )

    # A player who stays in the lineup but changes slot *type* still needs an
    # action from the owner; an identical-type reshuffle does not.
    for player_id in sorted(started_before & starting_now):
        before = slot_name.get(current_slot_of[player_id])
        after = slot_name.get(proposed_slot_of[player_id])
        if before is not None and after is not None and before != after:
            changes.append(
                LineupChange(
                    kind="move",
                    slot=after,
                    slot_id=proposed_slot_of[player_id],
                    player_in=player_id,
                    from_slot=before,
                )
            )

    return changes


def marginal_value(
    players: list[PlayerOption],
    slots: list[SlotSpec],
    player_id: str,
    objective: Objective = "balanced",
) -> float:
    """Points the optimal lineup loses if ``player_id`` disappears.

    This is the honest measure of a player's worth to *this* roster -- it is what
    makes a third startable RB worth less than his raw projection, and it is the
    basis for drop cost in `/waiver` and for both sides of a `/trade` evaluation.
    """
    baseline = optimize_lineup(players, slots, objective)
    without = optimize_lineup([p for p in players if p.player_id != player_id], slots, objective)
    return round(baseline.total_median - without.total_median, 3)
