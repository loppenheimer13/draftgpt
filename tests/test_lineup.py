"""Lineup optimizer. The correctness bar here is *exactness*, not plausibility.

A greedy "best player into the most restrictive open slot" heuristic passes
casual inspection and then quietly costs points in the exact situations that
matter. These tests pin the cases where greedy and optimal diverge.
"""

from __future__ import annotations

import pytest

from draftgpt.evaluation.lineup import (
    PlayerOption,
    SlotSpec,
    lineup_delta,
    marginal_value,
    optimize_lineup,
)


def slots(*specs: tuple[str, tuple[str, ...]]) -> list[SlotSpec]:
    return [
        SlotSpec(
            slot_id=f"s{i}", slot=slot, ordinal=i,
            eligible_positions=eligible, display_order=i,
        )
        for i, (slot, eligible) in enumerate(specs)
    ]


def player(pid: str, position: str, median: float, **kw) -> PlayerOption:
    return PlayerOption(
        player_id=pid,
        name=kw.pop("name", pid),
        positions=(position,),
        median=median,
        floor=kw.pop("floor", median * 0.6),
        ceiling=kw.pop("ceiling", median * 1.5),
        **kw,
    )


STANDARD_SLOTS = slots(
    ("QB", ("QB",)),
    ("RB", ("RB",)),
    ("RB", ("RB",)),
    ("WR", ("WR",)),
    ("WR", ("WR",)),
    ("TE", ("TE",)),
    ("FLEX", ("RB", "WR", "TE")),
)


def total_of(solution, players):
    by_id = {p.player_id: p for p in players}
    return round(sum(by_id[pid].median for pid in solution.starters()), 3)


def test_flex_displacement_beats_greedy():
    """The canonical case greedy gets wrong.

    Greedy assigns the top WR to a WR slot, the second WR to the other, then
    puts the best remaining *any* player in FLEX. Here the optimal answer needs
    a WR in FLEX so that all three WRs start, because the best remaining RB is
    worse than the third WR.
    """
    players = [
        player("qb", "QB", 20),
        player("rb1", "RB", 18),
        player("rb2", "RB", 12),
        player("rb3", "RB", 9),
        player("wr1", "WR", 17),
        player("wr2", "WR", 16),
        player("wr3", "WR", 15),
        player("te1", "TE", 10),
    ]
    solution = optimize_lineup(players, STANDARD_SLOTS)
    starters = solution.starters()

    assert {"qb", "rb1", "rb2", "wr1", "wr2", "wr3", "te1"} == starters
    assert "rb3" not in starters, "RB3 (9) must not take FLEX over WR3 (15)"
    assert total_of(solution, players) == pytest.approx(108.0)


def test_te_premium_flex_assignment():
    """A TE good enough to out-score every flex-eligible RB/WR must reach FLEX."""
    players = [
        player("qb", "QB", 20),
        player("rb1", "RB", 14), player("rb2", "RB", 13), player("rb3", "RB", 5),
        player("wr1", "WR", 15), player("wr2", "WR", 12), player("wr3", "WR", 6),
        player("te1", "TE", 11), player("te2", "TE", 10),
    ]
    solution = optimize_lineup(players, STANDARD_SLOTS)
    assert "te2" in solution.starters()
    assert "rb3" not in solution.starters()


def test_optimizer_is_exact_against_brute_force():
    """Cross-check the matroid-greedy result against exhaustive search."""
    from itertools import permutations

    players = [
        player("qb", "QB", 19),
        player("rb1", "RB", 16), player("rb2", "RB", 11), player("rb3", "RB", 10),
        player("wr1", "WR", 15), player("wr2", "WR", 14), player("wr3", "WR", 13),
        player("te1", "TE", 12),
    ]
    solution = optimize_lineup(players, STANDARD_SLOTS)

    best = 0.0
    by_id = {p.player_id: p for p in players}
    for combination in permutations(players, len(STANDARD_SLOTS)):
        if all(
            slot.accepts(option.positions)
            for slot, option in zip(STANDARD_SLOTS, combination, strict=True)
        ):
            best = max(best, sum(o.median for o in combination))

    assert total_of(solution, players) == pytest.approx(round(best, 3))
    assert by_id  # sanity


def test_bye_and_out_players_are_excluded_not_zeroed():
    """A player on bye must never occupy a slot, even if nothing else is
    available -- an empty slot is honest, a 0-point starter is misleading."""
    players = [
        player("qb", "QB", 20),
        player("wr1", "WR", 14, on_bye=True),
        player("wr2", "WR", 12, status="O"),
        player("wr3", "WR", 8),
    ]
    spec = slots(("QB", ("QB",)), ("WR", ("WR",)), ("WR", ("WR",)))
    solution = optimize_lineup(players, spec)

    assert solution.starters() == {"qb", "wr3"}
    assert len(solution.unfilled_slots) == 1


def test_locked_players_are_pinned_to_their_slot():
    """Once a game kicks off the platform will not accept a change; advice that
    ignores that is worse than useless."""
    players = [
        player("wr1", "WR", 5, is_locked=True, locked_slot_id="s0"),
        player("wr2", "WR", 25),
    ]
    spec = slots(("WR", ("WR",)), ("WR", ("WR",)))
    solution = optimize_lineup(players, spec)
    assert solution.assignments["s0"] == "wr1"
    assert solution.assignments["s1"] == "wr2"


def test_objective_changes_the_lineup():
    """Floor and ceiling objectives must actually diverge, or the risk
    preference is decorative."""
    players = [
        player("safe", "WR", 12.0, floor=11.0, ceiling=13.0),
        player("swing", "WR", 11.5, floor=2.0, ceiling=30.0),
    ]
    spec = slots(("WR", ("WR",)))
    assert optimize_lineup(players, spec, "floor").starters() == {"safe"}
    assert optimize_lineup(players, spec, "ceiling").starters() == {"swing"}


def test_result_is_deterministic_under_ties():
    """Identical inputs must reproduce an identical lineup, or a replayed
    snapshot could contradict the advice it is meant to explain."""
    players = [player(f"wr{i}", "WR", 10.0) for i in range(6)]
    spec = slots(("WR", ("WR",)), ("WR", ("WR",)))
    first = optimize_lineup(players, spec).assignments
    for _ in range(5):
        assert optimize_lineup(list(reversed(players)), spec).assignments == first


def test_marginal_value_reflects_lineup_impact_not_raw_points():
    """A third RB behind two better ones is worth far less than his projection
    -- this is the number that makes drop-cost and trade evaluation honest."""
    players = [
        player("rb1", "RB", 18), player("rb2", "RB", 16), player("rb3", "RB", 14),
        player("wr1", "WR", 15), player("wr2", "WR", 13), player("wr3", "WR", 12),
        player("qb", "QB", 20), player("te1", "TE", 10),
    ]
    starter_loss = marginal_value(players, STANDARD_SLOTS, "rb1")
    depth_loss = marginal_value(players, STANDARD_SLOTS, "rb3")
    assert starter_loss > depth_loss
    assert depth_loss < 14.0


def test_delta_ignores_permutation_between_identical_slots():
    """Swapping two RBs between the two RB slots is not a change the owner has
    to act on; reporting it as one is noise."""
    spec = slots(("RB", ("RB",)), ("RB", ("RB",)))
    current = {"s0": "a", "s1": "b"}
    proposed = {"s0": "b", "s1": "a"}
    assert lineup_delta(current, proposed, spec) == []


def test_delta_reports_real_swaps_and_moves():
    spec = slots(("WR", ("WR",)), ("FLEX", ("RB", "WR", "TE")))
    current = {"s0": "a"}
    proposed = {"s0": "b", "s1": "a"}
    changes = lineup_delta(current, proposed, spec)
    kinds = {c.kind for c in changes}
    assert "start" in kinds  # b enters the lineup
    assert "move" in kinds   # a shifts WR -> FLEX
