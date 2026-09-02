"""Freshness policy: how stale an input may be before the show says so."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from huddle.domain.enums import FreshnessClass, SeasonPhase
from huddle.domain.freshness import (
    FreshnessInput,
    evaluate_freshness,
    infer_season_phase,
    max_age_for,
    next_publish_time,
)

NOW = datetime(2026, 9, 1, 12, tzinfo=UTC)


def source(age_hours: float, *, capability="scoreboard", cls=FreshnessClass.RAPID,
           health="healthy") -> FreshnessInput:
    return FreshnessInput(
        provider="espn",
        capability=capability,
        freshness_class=str(cls),
        last_success_at=NOW - timedelta(hours=age_hours),
        health=health,
    )


def test_scores_go_stale_faster_on_a_game_day() -> None:
    assert max_age_for(FreshnessClass.RAPID, SeasonPhase.GAME_DAY) < max_age_for(
        FreshnessClass.RAPID, SeasonPhase.REGULAR_SEASON
    )


def test_fresh_data_reports_fresh() -> None:
    block, confidence = evaluate_freshness([source(1)], phase=SeasonPhase.REGULAR_SEASON,
                                           now=NOW)
    assert block.status == "fresh"
    assert confidence == 1.0
    assert not block.warnings


def test_old_data_degrades_rather_than_blocking() -> None:
    """A show built on slightly old scores is far better than no show."""
    block, confidence = evaluate_freshness([source(30)], phase=SeasonPhase.REGULAR_SEASON,
                                           now=NOW)
    assert block.status in ("degraded", "stale")
    assert confidence < 1.0
    assert block.warnings


def test_a_never_ingested_source_is_reported_and_warned_about() -> None:
    """The per-source report says stale; the overall block only degrades,
    because one missing non-critical feed should not condemn the whole show."""
    never = FreshnessInput("espn", "scoreboard", str(FreshnessClass.RAPID), None)
    block, confidence = evaluate_freshness([never], now=NOW)
    assert block.sources[0].status == "stale"
    assert block.status == "degraded"
    assert "never successfully ingested" in block.warnings[0]
    assert confidence < 1.0


def test_a_failing_provider_degrades_even_when_recent() -> None:
    block, _ = evaluate_freshness([source(0.5, health="failing")], now=NOW)
    assert block.status == "degraded"


def test_critical_capability_stale_marks_the_whole_section_stale() -> None:
    stale = source(400, capability="scoreboard")
    block, _ = evaluate_freshness([stale], section="today_in_sports",
                                  phase=SeasonPhase.GAME_DAY, now=NOW)
    assert block.status == "stale"
    assert block.warnings[0].startswith("CRITICAL")


def test_game_day_is_inferred_from_games_not_the_calendar() -> None:
    assert infer_season_phase(NOW, has_games_today=True) == SeasonPhase.GAME_DAY


def test_next_publish_anchors_on_kickoff_when_there_is_one() -> None:
    kickoff = NOW + timedelta(hours=6)
    assert next_publish_time(SeasonPhase.GAME_DAY, NOW, kickoff_at=kickoff) < kickoff
