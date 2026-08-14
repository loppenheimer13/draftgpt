"""League strategy derivation.

The value of this module is that it inverts generic advice when the settings
demand it. These tests pin the inversions, because a rule that always says the
same thing is worse than no rule.
"""

from __future__ import annotations

from draftgpt.evaluation.league_shape import analyze_league
from draftgpt.evaluation.lineup import SlotSpec
from draftgpt.evaluation.scoring import ScoringRules

PPR = {"rec": 1.0, "rec_yd": 0.1, "rec_td": 6.0, "pass_td": 4.0}
STANDARD = {**PPR, "rec": 0.0}


def slot(slot_id, name, ordinal, eligible, order):
    return SlotSpec(slot_id, name, ordinal, eligible, order)


SINGLE_QB = [
    slot("a", "QB", 0, ("QB",), 0),
    slot("b", "RB", 0, ("RB",), 1),
    slot("c", "RB", 1, ("RB",), 2),
    slot("d", "WR", 0, ("WR",), 3),
    slot("e", "WR", 1, ("WR",), 4),
    slot("f", "TE", 0, ("TE",), 5),
    slot("g", "FLEX", 0, ("RB", "WR", "TE"), 6),
]
SUPERFLEX = [*SINGLE_QB, slot("h", "SFLEX", 0, ("QB", "RB", "WR", "TE"), 7)]


def find(shape, key):
    return next((f for f in shape.findings if f.key == key), None)


def test_single_qb_says_wait_on_quarterback():
    shape = analyze_league(ScoringRules.from_config(PPR), SINGLE_QB, team_count=12)
    finding = find(shape, "single_qb")
    assert finding is not None
    assert "replaceable" in finding.headline
    assert finding.evidence["superflex"] is False


def test_superflex_inverts_the_quarterback_advice():
    """The single most important inversion. Generic 'wait on QB' advice is
    actively harmful in superflex."""
    shape = analyze_league(ScoringRules.from_config(PPR), SUPERFLEX, team_count=12)
    assert shape.is_superflex
    finding = find(shape, "superflex_qb")

    assert finding is not None
    assert finding.impact == "defining"
    assert "scarcest" in finding.headline
    assert find(shape, "single_qb") is None, "must not emit both QB findings"


def test_superflex_raises_quarterback_demand():
    single = analyze_league(ScoringRules.from_config(PPR), SINGLE_QB, team_count=12)
    superflex = analyze_league(ScoringRules.from_config(PPR), SUPERFLEX, team_count=12)
    assert superflex.demand["QB"] > single.demand["QB"] * 1.5


def test_ppr_and_standard_produce_different_findings():
    ppr = find(analyze_league(ScoringRules.from_config(PPR), SINGLE_QB, 12), "reception_scoring")
    std = find(
        analyze_league(ScoringRules.from_config(STANDARD), SINGLE_QB, 12), "reception_scoring"
    )
    assert "Full PPR" in ppr.headline
    assert "Standard" in std.headline
    assert "mislead" in std.detail, "standard leagues must warn that rankings assume PPR"


def test_six_point_passing_touchdowns_are_flagged():
    scoring = ScoringRules.from_config({**PPR, "pass_td": 6.0})
    finding = find(analyze_league(scoring, SINGLE_QB, 12), "pass_td_six")
    assert finding is not None
    assert finding.evidence["points_per_passing_td"] == 6.0


def test_flex_count_drives_wide_receiver_demand():
    two_flex = [*SINGLE_QB, slot("h", "FLEX", 1, ("RB", "WR", "TE"), 7)]
    one = analyze_league(ScoringRules.from_config(PPR), SINGLE_QB, 12)
    two = analyze_league(ScoringRules.from_config(PPR), two_flex, 12)
    assert two.demand["WR"] > one.demand["WR"]
    assert find(two, "flex_demand").evidence["flex_slots_per_team"] == 2


def test_team_count_scales_demand_linearly():
    small = analyze_league(ScoringRules.from_config(PPR), SINGLE_QB, 8)
    large = analyze_league(ScoringRules.from_config(PPR), SINGLE_QB, 14)
    assert large.demand["RB"] > small.demand["RB"]


def test_shallow_bench_discourages_speculation():
    shallow = analyze_league(ScoringRules.from_config(PPR), SINGLE_QB, 12, bench_slots=3)
    deep = analyze_league(ScoringRules.from_config(PPR), SINGLE_QB, 12, bench_slots=8)
    assert "almost no room" in find(shallow, "bench_depth").detail
    assert find(deep, "bench_depth").impact == "high"


def test_bonuses_are_surfaced_with_their_thresholds():
    scoring = ScoringRules.from_config(
        {**PPR, "bonuses": [{"stat": "rush_yd", "threshold": 100, "points": 3.0}]}
    )
    finding = find(analyze_league(scoring, SINGLE_QB, 12), "bonuses")
    assert finding is not None
    assert finding.evidence["bonuses"][0]["threshold"] == 100


def test_no_bonuses_emits_no_bonus_finding():
    shape = analyze_league(ScoringRules.from_config(PPR), SINGLE_QB, 12)
    assert find(shape, "bonuses") is None


def test_kicker_and_defense_flagged_as_streaming_only_when_started():
    without = analyze_league(ScoringRules.from_config(PPR), SINGLE_QB, 12)
    assert find(without, "stream_positions") is None

    with_k = [*SINGLE_QB, slot("k", "K", 0, ("K",), 7)]
    shape = analyze_league(ScoringRules.from_config(PPR), with_k, 12)
    assert "last" in find(shape, "stream_positions").headline


def test_faab_finding_only_when_a_budget_exists():
    assert find(analyze_league(ScoringRules.from_config(PPR), SINGLE_QB, 12), "faab") is None
    shape = analyze_league(ScoringRules.from_config(PPR), SINGLE_QB, 12, faab_budget=100)
    assert "100" in find(shape, "faab").headline


def test_findings_are_ordered_by_impact():
    shape = analyze_league(ScoringRules.from_config(PPR), SUPERFLEX, 12, bench_slots=6,
                           faab_budget=100, playoff_weeks=[15, 16, 17])
    impacts = [f.impact for f in shape.findings]
    order = {"defining": 0, "high": 1, "moderate": 2, "minor": 3}
    assert impacts == sorted(impacts, key=lambda i: order[i])
    assert shape.findings[0].impact == "defining"


def test_every_finding_carries_evidence():
    """A claim without the number behind it is just an opinion."""
    shape = analyze_league(
        ScoringRules.from_config(PPR), SUPERFLEX, 12, bench_slots=6, faab_budget=100
    )
    for finding in shape.findings:
        assert finding.evidence, f"{finding.key} has no evidence"
        assert finding.detail
