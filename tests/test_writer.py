"""The writer turns facts into speech. These tests guard how it sounds.

Every case here is a defect that shipped into a real generated show during
development: "The Liverpool", a score read as "six dash three", a host handing
over to somebody who was not up next.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from huddle.content import Factoid, Moment
from huddle.domain.enums import HOST_PROFILES, ShowSegment
from huddle.show.brief import BirthdayLine, GameLine, ShowBrief, StoryLine, TeamUpdate
from huddle.show.writer import (
    _article,
    _say_record,
    _short,
    _soften,
    estimate_minutes,
    write_show,
)


def game(**kw) -> GameLine:
    base = dict(
        league_key="mlb", league_spoken="Major League Baseball",
        home_name="Washington Nationals", away_name="Atlanta Braves",
        home_score=None, away_score=None, state="pre", when="tonight at 7",
    )
    base.update(kw)
    return GameLine(**base)


def team(**kw) -> TeamUpdate:
    base = dict(
        team_id="t1", name="Atlanta Braves", short_name="Braves", nickname=None,
        league_key="mlb", league_spoken="Major League Baseball",
    )
    base.update(kw)
    return TeamUpdate(**base)


def brief(**kw) -> ShowBrief:
    base = dict(
        family_id="f1", show_date=datetime(2026, 9, 1).date(),
        generated_at=datetime(2026, 9, 1, tzinfo=UTC), weekday="Tuesday",
    )
    base.update(kw)
    return ShowBrief(**base)


# -- language --------------------------------------------------------------
@pytest.mark.parametrize(
    ("name", "expected"),
    [("Braves", "the Braves"), ("Liverpool", "Liverpool"),
     ("Aces", "the Aces"), ("Manchester City", "Manchester City")],
)
def test_article_only_where_english_uses_one(name: str, expected: str) -> None:
    assert _article(name) == expected


@pytest.mark.parametrize(
    ("full", "expected"),
    [("Washington Nationals", "the Nationals"), ("Manchester City", "Manchester City"),
     ("Liverpool", "Liverpool"), ("Las Vegas Aces", "the Aces")],
)
def test_opponent_shortening(full: str, expected: str) -> None:
    assert _short(full) == expected


def test_scorelines_become_words() -> None:
    """A synthesiser reads "6-3" as "six dash three"."""
    assert "six to three" in _soften("Marlins beat Royals 6-3")
    assert "dash" not in _soften("Marlins beat Royals 6-3")


def test_apostrophes_survive_softening() -> None:
    """Stripping them turns "won't" into "wont"."""
    assert "won't" in _soften("It won't be on WNBA courts")


def test_ambiguous_records_are_not_guessed() -> None:
    """A three-part soccer record is win-draw-loss, not win-loss-draw. Rather
    than say something false, the show says nothing about it."""
    assert _say_record("82-57", "mlb") == "82 wins and 57 losses"
    assert _say_record("1-1-1", "epl") is None
    assert _say_record("0-2", "mlb") == "no wins and 2 losses"


# -- show shape ------------------------------------------------------------
def test_show_opens_and_signs_off() -> None:
    segments = write_show(
        brief(factoid=Factoid(question="What is offside?", answer="A soccer rule.")),
        segments=[ShowSegment.ROOKIE_FACTOID],
    )
    assert segments
    assert segments[0].text.startswith("Huddle up!")
    assert segments[-1].text.rstrip().endswith("Go play something.")


def test_hosts_alternate_and_handoffs_name_the_right_person() -> None:
    """A host introducing somebody who is not up next is the most obvious
    possible tell that nobody listened to the output."""
    segments = write_show(
        brief(
            teams=[team(last_game=game(state="post", home_score=3, away_score=5))],
            today_stories=[StoryLine("mlb", "Major League Baseball", "Braves win", "They won.")],
            moment=Moment(title="A moment", story="It happened."),
            factoid=Factoid(question="Why?", answer="Because."),
        )
    )
    assert len(segments) >= 3
    for current, following in zip(segments, segments[1:], strict=False):
        assert current.host != following.host, "two tracks in a row with one voice"
        next_name = HOST_PROFILES[following.host]["name"]
        assert next_name in current.text, "handoff names the wrong host"


def test_team_paragraph_always_names_its_team() -> None:
    """A listener who missed the handoff has no other way to know who this is
    about -- a paragraph that opens "Next up, they play..." is unusable."""
    segments = write_show(
        brief(teams=[team(name="Liverpool", short_name="Liverpool", next_game=game(
            home_name="Ipswich Town", away_name="Liverpool", when="Friday at 3"))]),
        segments=[ShowSegment.YOUR_TEAMS],
    )
    text = segments[0].text
    assert "Liverpool play" in text
    assert "The Liverpool" not in text


def test_result_is_told_from_the_family_point_of_view() -> None:
    segments = write_show(
        brief(teams=[team(last_game=game(
            state="post", home_name="Washington Nationals", away_name="Atlanta Braves",
            home_score=9, away_score=5))]),
        segments=[ShowSegment.YOUR_TEAMS],
    )
    text = segments[0].text.replace("The Braves", "the Braves")
    assert "the Braves lost to the Nationals, nine to five" in text


def test_empty_day_does_not_fake_excitement() -> None:
    segments = write_show(brief(), segments=[ShowSegment.YOUR_TEAMS])
    assert "plenty to get through" not in segments[0].text
    assert "slow one out there" in segments[0].text


def test_weekend_edition_only_airs_late_week() -> None:
    tuesday = brief(show_date=datetime(2026, 9, 1).date())   # a Tuesday
    friday = brief(show_date=datetime(2026, 9, 4).date())
    names = lambda b: {s.segment for s in write_show(  # noqa: E731
        b, segments=[ShowSegment.WEEKEND_EDITION, ShowSegment.ON_THIS_DAY],
    )}
    assert ShowSegment.WEEKEND_EDITION not in names(tuesday)
    assert ShowSegment.WEEKEND_EDITION in names(friday)


def test_target_length_caps_the_show_rather_than_padding_it() -> None:
    """Target minutes is a ceiling, not a quota.

    With more material than fits, a shorter target must produce a shorter show.
    With less material than fits, both targets produce the same show -- the
    writer never pads, because there is no honest way to invent sports news.
    """
    rich = [
        team(
            team_id=f"t{i}",
            last_game=game(state="post", home_score=i, away_score=1),
            next_game=game(when="tomorrow evening at 7", broadcast="ESPN"),
            record_summary="82-57",
            story="Braves clinch a spot with a late win over the Mets",
            one_thing="That was not close for long.",
        )
        for i in range(12)
    ]
    short = write_show(brief(teams=rich), target_minutes=3)
    long = write_show(brief(teams=rich), target_minutes=9)
    assert estimate_minutes(long) > estimate_minutes(short)

    sparse = [team(team_id="t1", last_game=game(state="post", home_score=3, away_score=1))]
    assert estimate_minutes(write_show(brief(teams=sparse), target_minutes=9)) == pytest.approx(
        estimate_minutes(write_show(brief(teams=sparse), target_minutes=3))
    )


def test_birthday_club_reads_as_a_celebration() -> None:
    segments = write_show(
        brief(birthdays=[BirthdayLine(
            name="Ronald Acuna", team_name="Braves", league_spoken="Major League Baseball",
            position="Outfielder", age=28, birth_place="La Sabana, Venezuela")]),
        segments=[ShowSegment.BIRTHDAY_CLUB],
    )
    text = segments[0].text
    assert "Ronald Acuna" in text and "turns 28" in text
    assert "Happy birthday" in text


def test_sentences_are_capitalised() -> None:
    segments = write_show(
        brief(teams=[team(last_game=game(state="post", home_score=3, away_score=9))]),
        segments=[ShowSegment.YOUR_TEAMS],
    )
    import re

    assert not re.search(r"[.!?]\s+[a-z]", segments[0].text)
