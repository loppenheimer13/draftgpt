"""The safety filter is the product's core promise, so it gets the most tests.

Each case below is a real headline shape that appeared in a live feed during
development. The block cases are the ones that matter: every one of them
reached the writer at some point before the gate that now stops it existed.
"""

from __future__ import annotations

import pytest

from huddle.domain.safety import SafetyFilter, SafetyPolicy


@pytest.fixture
def filt() -> SafetyFilter:
    return SafetyFilter(SafetyPolicy())


def story(headline: str, summary: str = "", story_type: str = "story") -> dict:
    return {
        "headline": headline,
        "summary": summary or f"{headline} and some further detail for length.",
        "type": story_type,
        "categories": [],
    }


@pytest.mark.parametrize(
    ("headline", "category"),
    [
        ("Every team's odds to win the World Series", "betting"),
        ("Star quarterback arrested overnight downtown", "legal"),
        ("Ex-QB pleads no contest after a night out", "legal"),
        ("Pitcher suspended after a positive test", "substances"),
        ("Star forward carted off with a torn ACL", "graphic_injury"),
        ("Guardians add player to roster after fatal crash", "death_and_harm"),
        ("Fantasy football Knockout league rankings: players to draft", "fantasy_and_analysis"),
        ("Lions place running back on injured reserve", "injury_status"),
        ("Man City sign midfielder for joint-record fee", "business"),
        ("Chelsea fume after transfer U-turn - sources", "rumour"),
    ],
)
def test_blocks_adult_topics(filt: SafetyFilter, headline: str, category: str) -> None:
    result = filt.check_story(story(headline))
    assert not result.allowed, f"{headline!r} should not reach a children's show"
    assert result.category == category


@pytest.mark.parametrize(
    "headline",
    [
        "Trevino singles in go-ahead run in 8th as Reds beat Padres 4-3",
        "Shepard has 22nd double-double this season, Wings beat Sun 97-71",
        "Anaheim Ducks to retire former captain's No. 15",
        "Why are there no WNBA games for two weeks? More about the FIBA break",
        "Dallas Wings vs. Connecticut Sun - Game Highlights",
    ],
)
def test_allows_kid_friendly_sport(filt: SafetyFilter, headline: str) -> None:
    result = filt.check_story(story(headline))
    assert result.allowed, f"{headline!r} is exactly what the show is for"
    assert result.shape is not None


def test_unrecognised_shape_is_dropped(filt: SafetyFilter) -> None:
    """The gate that makes the filter sound: anything not positively
    recognised is dropped, rather than admitted because no word matched."""
    result = filt.check_story(
        story("Analyst discusses the club's long-term organisational direction")
    )
    assert not result.allowed
    assert result.category == "unrecognized_shape"


def test_recap_type_needs_no_shape_evidence(filt: SafetyFilter) -> None:
    result = filt.check_story(story("A quiet afternoon at the ballpark", story_type="recap"))
    assert result.allowed
    assert result.shape == "recap"


def test_thin_stories_are_dropped(filt: SafetyFilter) -> None:
    assert not filt.check_story({"headline": "Update", "summary": "", "type": "news"}).allowed


def test_caution_marks_but_does_not_block(filt: SafetyFilter) -> None:
    result = filt.check_story(
        story("Veteran captain retires after 18 seasons and a championship")
    )
    assert result.allowed
    assert result.caution, "a retirement should air, but never as the lead"


def test_check_text_guards_finished_copy(filt: SafetyFilter) -> None:
    """The last gate: copy that was clean when gathered can be rewritten into
    something that is not."""
    assert filt.check_text("The Braves beat the Nationals five to three.").allowed
    assert not filt.check_text("Bet on the Braves tonight.").allowed


def test_custom_policy_cannot_disable_a_default(tmp_path) -> None:
    """A custom safety file adds to the defaults; it must not subtract."""
    from huddle.domain.safety import load_policy

    (tmp_path / "safety.yaml").write_text(
        "blocked_terms:\n  betting: []\n  local: ['ballyhoo']\n"
    )
    policy = load_policy(tmp_path / "safety.yaml")
    filt = SafetyFilter(policy)
    assert not filt.check_text("place a wager on tonight's game").allowed
    assert not filt.check_text("total ballyhoo").allowed
