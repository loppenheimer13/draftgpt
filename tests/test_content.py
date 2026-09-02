"""Curated content and the daily rotation."""

from __future__ import annotations

from datetime import date

from huddle.content import factoid_for, load_factoids, load_moments, moment_for


def test_content_files_load() -> None:
    assert len(load_moments()) >= 30
    assert len(load_factoids()) >= 30


def test_every_entry_is_written_for_a_child() -> None:
    """Short enough to hold attention, long enough to be worth saying."""
    for moment in load_moments():
        assert 40 <= len(moment.story) <= 400, moment.title
    for factoid in load_factoids():
        assert 40 <= len(factoid.answer) <= 400, factoid.question


def test_a_real_anniversary_is_used_when_there_is_one() -> None:
    moment = moment_for(date(2026, 4, 15))
    assert moment.is_dated
    assert "Jackie Robinson" in moment.title


def test_a_day_with_no_anniversary_still_gets_a_moment() -> None:
    """And it must not claim to be an anniversary -- the host says
    'here's a moment worth remembering' instead."""
    moment = moment_for(date(2026, 6, 3))
    assert moment is not None
    assert not moment.is_dated


def test_selection_is_deterministic_across_households() -> None:
    day = date(2026, 7, 14)
    assert moment_for(day).title == moment_for(day).title
    assert factoid_for(day).question == factoid_for(day).question


def test_consecutive_days_do_not_repeat() -> None:
    days = [date(2026, 7, d) for d in range(1, 15)]
    questions = [factoid_for(d).question for d in days]
    assert len(set(questions)) >= len(days) - 2, "the rotation is too repetitive"

    titles = [moment_for(d).title for d in days]
    assert len(set(titles)) >= len(days) - 2
