"""Draft board: tier detection, value over replacement, and availability.

Tier detection is the highest-leverage thing on this board. If it fires on
every player it is noise, and if it misses a real cliff the board actively
misleads. These tests pin both failure directions.
"""

from __future__ import annotations

import pytest

from draftgpt.evaluation.board import (
    BoardPlayer,
    build_board,
    picks_until_next_turn,
    survival_probability,
)
from draftgpt.evaluation.league_shape import analyze_league
from draftgpt.evaluation.lineup import SlotSpec
from draftgpt.evaluation.replacement import detect_tier_cliffs
from draftgpt.evaluation.scoring import ScoringRules

PPR = ScoringRules.from_config({"rec": 1.0, "rec_yd": 0.1, "rec_td": 6.0})

STANDARD_SLOTS = [
    SlotSpec("s0", "QB", 0, ("QB",), 0),
    SlotSpec("s1", "RB", 0, ("RB",), 1),
    SlotSpec("s2", "RB", 1, ("RB",), 2),
    SlotSpec("s3", "WR", 0, ("WR",), 3),
    SlotSpec("s4", "WR", 1, ("WR",), 4),
    SlotSpec("s5", "TE", 0, ("TE",), 5),
    SlotSpec("s6", "FLEX", 0, ("RB", "WR", "TE"), 6),
    # K and DST are included because replacement level is only defined for
    # positions the league actually starts: with no K slot, the replacement
    # kicker is the best kicker and every kicker's VOR collapses to zero.
    SlotSpec("s7", "K", 0, ("K",), 7),
    SlotSpec("s8", "DST", 0, ("DST",), 8),
]


# --------------------------------------------------------------------------
# Tier detection
# --------------------------------------------------------------------------
def test_perfectly_linear_curve_has_no_tiers():
    """Even spacing means no tier boundaries. A detector that fires here would
    flag every player on a real board."""
    assert detect_tier_cliffs([100 - 3 * i for i in range(20)], "WR") == []


def test_steep_top_with_flat_tail_does_not_flag_the_whole_top():
    """The real-world curve shape. A global-mean baseline is dragged down by the
    flat tail and flags every steep-but-normal gap at the top."""
    curve = [346 - 5.8 * i for i in range(20)] + [230 - 0.4 * i for i in range(32)]
    breaks = detect_tier_cliffs(curve, "RB")

    assert len(breaks) <= 2, f"expected the steep/flat boundary only, got {len(breaks)}"
    assert breaks[0].after_rank == 20, "the cliff is where steep meets flat"


def test_genuine_cliff_is_found():
    curve = [346, 340, 334, 328, 322, 316, 280, 275, 270, 265, 260, 255, 250, 245]
    breaks = detect_tier_cliffs(curve, "RB")
    assert [b.after_rank for b in breaks] == [6]
    assert breaks[0].gap == pytest.approx(36.0)


def test_multiple_distinct_cliffs_are_all_found():
    curve = [300, 295, 290, 240, 235, 230, 180, 175, 170, 165, 160, 155]
    assert [b.after_rank for b in detect_tier_cliffs(curve, "TE")] == [3, 6]


def test_tiny_gaps_in_a_flat_curve_are_not_cliffs():
    """A 0.3-point gap is not a tier break however unusual it is locally."""
    curve = [100.0, 99.9, 99.8, 99.7, 99.4, 99.3, 99.2, 99.1, 99.0, 98.9]
    assert detect_tier_cliffs(curve, "K") == []


def test_too_few_players_returns_no_tiers():
    assert detect_tier_cliffs([100, 90, 80], "QB") == []


# --------------------------------------------------------------------------
# Board construction
# --------------------------------------------------------------------------
def bp(pid, name, position, points, **kw) -> BoardPlayer:
    return BoardPlayer(
        player_id=pid, name=name, position=position, nfl_team=kw.pop("team", "SF"),
        projected_points=points, **kw
    )


def make_shape(team_count=12):
    return analyze_league(PPR, STANDARD_SLOTS, team_count=team_count, bench_slots=6)


def test_board_ranks_by_vor_not_raw_points():
    """A QB outscoring every RB in raw points is not automatically the top pick;
    what matters is the margin over the replacement at his own position."""
    shape = make_shape(team_count=2)
    players = (
        [bp(f"qb{i}", f"QB{i}", "QB", 400 - i * 2) for i in range(6)]
        + [bp(f"rb{i}", f"RB{i}", "RB", 300 - i * 40) for i in range(6)]
    )
    board = build_board(players, shape)
    top = board.entries[0]

    assert top.player.position == "RB", "the RB has the larger edge over his replacement"
    assert top.player.projected_points < board.find("qb0").player.projected_points


def test_replacement_level_scales_with_league_size():
    """A deeper league has a worse replacement player, so the same star is worth
    more. This is why advice cannot be league-agnostic."""
    players = [bp(f"rb{i}", f"RB{i}", "RB", 300 - i * 5) for i in range(40)]
    small = build_board(players, make_shape(team_count=8)).entries[0]
    large = build_board(players, make_shape(team_count=14)).entries[0]
    assert large.value_over_replacement > small.value_over_replacement


def test_position_ranks_and_tiers_are_assigned():
    shape = make_shape()
    players = [bp(f"wr{i}", f"WR{i}", "WR", 300 - i * 4) for i in range(30)]
    board = build_board(players, shape)
    wrs = board.by_position("WR")
    assert [e.position_rank for e in wrs[:3]] == [1, 2, 3]
    assert all(e.tier >= 1 for e in wrs)


def test_adp_delta_flags_value_falling_past_the_market():
    shape = make_shape()
    players = [
        bp("a", "Early Riser", "RB", 300, adp=1.0),
        bp("b", "Faller", "RB", 295, adp=40.0),
    ] + [bp(f"f{i}", f"F{i}", "RB", 200 - i, adp=50.0 + i) for i in range(30)]
    board = build_board(players, shape)

    faller = board.find("b")
    assert faller.adp_delta > 8, "should be flagged as available later than value implies"


def test_empty_projection_set_warns_rather_than_crashing():
    board = build_board([], make_shape())
    assert board.entries == []
    assert any("no projections" in w for w in board.warnings)


def test_missing_adp_is_warned():
    players = [bp(f"rb{i}", f"RB{i}", "RB", 300 - i * 5) for i in range(20)]
    board = build_board(players, make_shape())
    assert any("no ADP" in w for w in board.warnings)


def test_kicker_at_the_top_is_flagged_as_suspect_projections():
    """A kicker ranking above every skill player means the projections are
    broken. Saying so beats confidently recommending a kicker in round two."""
    shape = make_shape()
    # A broken stat-id map inflates the whole position, not one player -- which
    # is what gives the top kickers a large edge over their own replacement.
    players = [
        bp(f"k{i}", f"Inflated Kicker {i}", "K", 900 - i * 30, adp=float(i + 1))
        for i in range(20)
    ] + [bp(f"rb{i}", f"RB{i}", "RB", 300 - i * 5, adp=float(i + 25)) for i in range(30)]
    board = build_board(players, shape)

    assert board.entries[0].player.position == "K", "precondition: kickers top the board"
    assert any("SUSPECT PROJECTIONS" in w for w in board.warnings)
    assert any("verify-espn" in w for w in board.warnings)


def test_realistic_kicker_does_not_trigger_the_warning():
    shape = make_shape()
    players = [
        bp(f"k{i}", f"Normal Kicker {i}", "K", 140 - i * 1.5, adp=float(150 + i))
        for i in range(20)
    ] + [bp(f"rb{i}", f"RB{i}", "RB", 300 - i * 5, adp=float(i + 1)) for i in range(30)]
    board = build_board(players, shape)

    assert board.entries[0].player.position == "RB"
    assert not any("SUSPECT" in w for w in board.warnings)


# --------------------------------------------------------------------------
# Availability
# --------------------------------------------------------------------------
def test_survival_probability_decreases_with_later_picks():
    early = survival_probability(adp=20.0, adp_stdev=6.0, pick_number=10)
    late = survival_probability(adp=20.0, adp_stdev=6.0, pick_number=30)
    assert early > 0.9
    assert late < 0.1
    assert survival_probability(20.0, 6.0, 20) == pytest.approx(0.5, abs=0.02)


def test_survival_probability_needs_dispersion():
    assert survival_probability(20.0, None, 30) is None
    assert survival_probability(20.0, 0.0, 30) is None


def test_wider_dispersion_flattens_the_curve():
    tight = survival_probability(20.0, 2.0, 26)
    wide = survival_probability(20.0, 12.0, 26)
    assert wide > tight


def test_snake_turn_gap_is_widest_at_the_ends():
    """A manager on the turn waits far longer than one in the middle."""
    first = picks_until_next_turn(current_pick=1, draft_position=1, team_count=12)
    middle = picks_until_next_turn(current_pick=6, draft_position=6, team_count=12)
    assert first > middle


def test_linear_draft_gap_is_always_one_round():
    assert picks_until_next_turn(5, 5, 12, is_snake=False) == 12
