"""League-specific scoring. Every downstream number depends on these."""

from __future__ import annotations

import pytest

from draftgpt.evaluation.scoring import ScoringRules, score_breakdown, score_stat_line

PPR = {
    "rec": 1.0, "rec_yd": 0.1, "rec_td": 6.0,
    "rush_yd": 0.1, "rush_td": 6.0,
    "pass_yd": 0.04, "pass_td": 4.0, "pass_int": -2.0,
    "fum_lost": -2.0,
}
HALF_PPR = {**PPR, "rec": 0.5}
STANDARD = {**PPR, "rec": 0.0}


def test_reception_settings_change_the_answer():
    """The same stat line must score differently per league. This is the whole
    premise of a league-specific engine."""
    stats = {"rec": 8.0, "rec_yd": 90.0, "rec_td": 1.0}
    assert score_stat_line(stats, ScoringRules.from_config(PPR)) == pytest.approx(23.0)
    assert score_stat_line(stats, ScoringRules.from_config(HALF_PPR)) == pytest.approx(19.0)
    assert score_stat_line(stats, ScoringRules.from_config(STANDARD)) == pytest.approx(15.0)


def test_negative_stats_subtract():
    stats = {"pass_yd": 250.0, "pass_td": 2.0, "pass_int": 3.0, "fum_lost": 1.0}
    # 10 + 8 - 6 - 2
    assert score_stat_line(stats, ScoringRules.from_config(PPR)) == pytest.approx(10.0)


def test_threshold_bonus_applies_once_at_the_boundary():
    config = {**PPR, "bonuses": [{"stat": "rush_yd", "threshold": 100, "points": 3.0}]}
    rules = ScoringRules.from_config(config)
    assert score_stat_line({"rush_yd": 99.0}, rules) == pytest.approx(9.9)
    assert score_stat_line({"rush_yd": 100.0}, rules) == pytest.approx(13.0)
    # Not repeatable by default: 200 yards still earns exactly one bonus.
    assert score_stat_line({"rush_yd": 200.0}, rules) == pytest.approx(23.0)


def test_repeatable_bonus_scales_with_volume():
    config = {
        **PPR,
        "bonuses": [{"stat": "rush_yd", "threshold": 100, "points": 3.0, "repeatable": True}],
    }
    rules = ScoringRules.from_config(config)
    assert score_stat_line({"rush_yd": 250.0}, rules) == pytest.approx(25.0 + 6.0)


def test_points_allowed_tiers_replace_linear_scoring():
    """A defense scoring both per-point-allowed and by bracket would double
    count; the bracket table must win."""
    config = {
        "def_sack": 1.0,
        "def_pts_allowed": -0.5,
        "points_allowed_tiers": [
            {"min": 0, "max": 0, "points": 10.0},
            {"min": 1, "max": 6, "points": 7.0},
            {"min": 7, "max": 13, "points": 4.0},
        ],
    }
    rules = ScoringRules.from_config(config)
    assert score_stat_line({"def_sack": 3.0, "def_pts_allowed": 0.0}, rules) == pytest.approx(13.0)
    assert score_stat_line({"def_sack": 2.0, "def_pts_allowed": 10.0}, rules) == pytest.approx(6.0)


def test_points_allowed_outside_all_brackets_scores_zero():
    config = {"points_allowed_tiers": [{"min": 0, "max": 6, "points": 7.0}]}
    rules = ScoringRules.from_config(config)
    assert score_stat_line({"def_pts_allowed": 40.0}, rules) == pytest.approx(0.0)


def test_unknown_stats_are_ignored_not_crashed():
    rules = ScoringRules.from_config(PPR)
    assert score_stat_line({"rec": 2.0, "some_future_stat": 99.0}, rules) == pytest.approx(2.0)


def test_breakdown_explains_the_total():
    config = {**PPR, "bonuses": [{"stat": "rec_yd", "threshold": 100, "points": 3.0}]}
    rules = ScoringRules.from_config(config)
    stats = {"rec": 7.0, "rec_yd": 110.0, "rec_td": 1.0}
    breakdown = score_breakdown(stats, rules)
    assert breakdown["rec"] == pytest.approx(7.0)
    assert breakdown["bonus:rec_yd>=100"] == pytest.approx(3.0)
    assert sum(breakdown.values()) == pytest.approx(score_stat_line(stats, rules))


def test_ppr_detection():
    assert ScoringRules.from_config(PPR).is_ppr
    assert not ScoringRules.from_config(HALF_PPR).is_ppr
