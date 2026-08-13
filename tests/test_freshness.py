"""Freshness policy: staleness must be decision-dependent, and a missing
non-critical source must degrade a recommendation rather than block it."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from draftgpt.domain.enums import Capability, FreshnessClass, SeasonPhase
from draftgpt.domain.freshness import (
    FreshnessInput,
    evaluate_freshness,
    infer_season_phase,
    max_age_for,
    next_check_time,
)

NOW = datetime(2026, 9, 14, 16, 0, tzinfo=UTC)


def source(capability, freshness_class, age_hours, health="healthy"):
    return FreshnessInput(
        provider="test",
        capability=capability,
        freshness_class=freshness_class,
        last_success_at=NOW - timedelta(hours=age_hours),
        effective_at=NOW - timedelta(hours=age_hours),
        health=health,
    )


def test_same_age_is_fresh_preseason_and_stale_on_game_day():
    """The premise of decision-dependent freshness: one age, two verdicts."""
    stale_hours = 6
    preseason = max_age_for(FreshnessClass.RAPID, SeasonPhase.EARLY_PRESEASON)
    game_day = max_age_for(FreshnessClass.RAPID, SeasonPhase.GAME_DAY)
    assert preseason > stale_hours * 3600 > game_day


def test_noncritical_stale_source_degrades_but_still_answers():
    block, multiplier = evaluate_freshness(
        [source(Capability.WEATHER, FreshnessClass.RAPID, age_hours=40)],
        command="roster",
        phase=SeasonPhase.GAME_DAY,
        now=NOW,
    )
    assert block.status == "degraded"
    assert block.warnings and "weather" in block.warnings[0]
    assert 0 < multiplier < 1.0


def test_critical_stale_source_marks_the_whole_answer_stale():
    block, multiplier = evaluate_freshness(
        [source(Capability.LEAGUE_ROSTERS, FreshnessClass.LIVE, age_hours=48)],
        command="roster",
        phase=SeasonPhase.GAME_DAY,
        now=NOW,
    )
    assert block.status == "stale"
    assert "CRITICAL" in block.warnings[0]
    assert multiplier < 0.8


def test_never_ingested_source_is_stale():
    never = FreshnessInput(
        provider="test",
        capability=Capability.INJURIES,
        freshness_class=FreshnessClass.RAPID,
        last_success_at=None,
    )
    block, _ = evaluate_freshness([never], "roster", SeasonPhase.GAME_DAY, now=NOW)
    assert block.sources[0].status == "stale"
    assert "never successfully ingested" in block.sources[0].note


def test_fresh_sources_leave_confidence_untouched():
    block, multiplier = evaluate_freshness(
        [
            source(Capability.LEAGUE_ROSTERS, FreshnessClass.LIVE, age_hours=0.1),
            source(Capability.PROJECTIONS_WEEKLY, FreshnessClass.DAILY, age_hours=2),
        ],
        command="roster",
        phase=SeasonPhase.REGULAR_SEASON,
        now=NOW,
    )
    assert block.status == "fresh"
    assert block.warnings == []
    assert multiplier == 1.0


def test_failing_provider_degrades_even_when_recent():
    block, multiplier = evaluate_freshness(
        [source(Capability.INJURIES, FreshnessClass.RAPID, age_hours=0.2, health="failing")],
        command="roster",
        phase=SeasonPhase.GAME_DAY,
        now=NOW,
    )
    assert block.status == "degraded"
    assert multiplier < 1.0


def test_multiple_stale_sources_compound_the_penalty():
    _, one = evaluate_freshness(
        [source(Capability.WEATHER, FreshnessClass.RAPID, 40)],
        "roster", SeasonPhase.GAME_DAY, now=NOW,
    )
    _, two = evaluate_freshness(
        [
            source(Capability.WEATHER, FreshnessClass.RAPID, 40),
            source(Capability.USAGE, FreshnessClass.DAILY, 40),
        ],
        "roster", SeasonPhase.GAME_DAY, now=NOW,
    )
    assert two < one


def test_confidence_multiplier_has_a_floor():
    many = [source(f"cap{i}", FreshnessClass.LIVE, 1000) for i in range(20)]
    _, multiplier = evaluate_freshness(many, "roster", SeasonPhase.GAME_DAY, now=NOW)
    assert multiplier >= 0.2


def test_phase_inference_from_draft_proximity():
    draft_at = NOW + timedelta(hours=6)
    assert infer_season_phase(NOW, draft_at=draft_at) == SeasonPhase.DRAFT_DAY
    assert infer_season_phase(NOW, draft_at=NOW + timedelta(days=10)) == SeasonPhase.PRE_DRAFT


def test_phase_inference_from_league_status():
    assert infer_season_phase(NOW, league_status="in_season") == SeasonPhase.REGULAR_SEASON
    assert (
        infer_season_phase(NOW, league_status="in_season", has_games_today=True)
        == SeasonPhase.GAME_DAY
    )


def test_next_check_anchors_to_kickoff_when_one_is_near():
    """'Check again in 6 hours' is useless 20 minutes before kickoff."""
    kickoff = NOW + timedelta(hours=4)
    assert next_check_time(SeasonPhase.GAME_DAY, NOW, kickoff_at=kickoff) == kickoff - timedelta(
        minutes=90
    )


def test_next_check_falls_back_to_phase_interval_after_kickoff():
    past = NOW - timedelta(hours=1)
    result = next_check_time(SeasonPhase.GAME_DAY, NOW, kickoff_at=past)
    assert result == NOW + timedelta(hours=2)
