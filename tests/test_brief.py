"""Brief assembly: what the show is allowed to say."""

from __future__ import annotations

from datetime import timedelta

from conftest import add_athlete, add_game, add_story
from huddle.show.brief import build_brief


def test_result_and_next_game_are_separated(db, family, followed, now) -> None:
    add_game(db, followed, start_at=now - timedelta(hours=14), state="post",
             home=False, home_score=9, away_score=5)
    add_game(db, followed, start_at=now + timedelta(days=2), state="pre",
             home=True, broadcast="FOX")

    brief = build_brief(db, family, now=now)
    update = brief.teams[0]
    assert update.last_game is not None and update.last_game.has_score
    assert update.next_game is not None and update.next_game.state == "pre"
    assert update.next_game.broadcast == "FOX"


def test_a_pregame_zero_is_not_a_score(db, family, followed, now) -> None:
    """0-0 before kickoff means "not started", and saying it as a score would
    tell a child the game finished level."""
    add_game(db, followed, start_at=now + timedelta(hours=6), state="pre")
    brief = build_brief(db, family, now=now)
    assert brief.teams[0].next_game.has_score is False


def test_only_allowed_stories_reach_the_brief(db, family, followed, now) -> None:
    add_story(db, headline="Braves win a thriller over the Mets 4-3", verdict="allow")
    add_story(db, headline="Player arrested overnight", verdict="block")
    brief = build_brief(db, family, now=now)

    # A story about a followed team is told in that team's own segment, so
    # look across the whole brief rather than only at Today in Sports.
    spoken = [s.headline for s in brief.today_stories] + [
        t.story for t in brief.teams if t.story
    ]
    assert any("thriller" in h for h in spoken)
    assert not any("arrested" in h for h in spoken)


def test_a_team_story_is_not_repeated_in_today_in_sports(db, family, followed, now) -> None:
    """Hearing the same headline twice in one show sounds like a fault."""
    add_story(db, headline="Braves win a thriller over the Mets 4-3", verdict="allow")
    brief = build_brief(db, family, now=now)
    assert brief.teams[0].story is not None
    assert brief.teams[0].story not in [s.headline for s in brief.today_stories]


def test_blocked_stories_are_reported_to_the_parent(db, family, followed, now) -> None:
    """The safety page is only honest if the blocked list actually reaches it."""
    story = add_story(db, headline="Player arrested overnight", verdict="block")
    story.safety_category = "legal"
    story.safety_matched = "arrested"
    db.flush()

    brief = build_brief(db, family, now=now)
    assert any(item["reason"] == "legal" for item in brief.blocked_stories)


def test_gentle_stories_lead(db, family, followed, now) -> None:
    """A "caution" story may air, but it must never open the segment."""
    add_story(db, headline="Veteran captain retires after 18 seasons",
              verdict="allow", caution=True)
    add_story(db, headline="Mets top the Phillies 4-3 in extras",
              verdict="allow", caution=False)
    brief = build_brief(db, family, now=now)
    assert len(brief.today_stories) == 2
    assert brief.today_stories[0].caution is False


def test_birthday_club_is_scoped_to_followed_teams(db, family, followed, now) -> None:
    add_athlete(db, followed, name="Ronald Acuna", month=9, day=1, year=1997)
    add_athlete(db, followed, name="Someone Else", month=4, day=2)
    brief = build_brief(db, family, show_date=now.date(), now=now)
    assert [b.name for b in brief.birthdays] == ["Ronald Acuna"]
    assert brief.birthdays[0].age == 2026 - 1997


def test_curated_segments_carry_a_quiet_day(db, family, now) -> None:
    """A family with no teams and no news still gets a real show."""
    brief = build_brief(db, family, now=now)
    assert brief.teams == []
    assert brief.moment is not None
    assert brief.factoid is not None


def test_content_hash_ignores_timestamps(db, family, followed, now) -> None:
    """Two identical mornings must hash the same, so the second is skipped
    instead of republishing the same show."""
    add_game(db, followed, start_at=now - timedelta(hours=10), state="post",
             home=True, home_score=4, away_score=2)
    first = build_brief(db, family, show_date=now.date(), now=now)
    later = build_brief(db, family, show_date=now.date(), now=now + timedelta(minutes=30))
    assert first.content_hash() == later.content_hash()


def test_stale_games_fall_out_of_the_window(db, family, followed, now) -> None:
    add_game(db, followed, start_at=now - timedelta(days=5), state="post",
             home=True, home_score=1, away_score=0)
    brief = build_brief(db, family, now=now)
    assert brief.teams[0].last_game is None


def test_unknown_timezone_does_not_break_the_show(db, family, followed, now) -> None:
    family.timezone = "Mars/Olympus_Mons"
    db.flush()
    brief = build_brief(db, family, now=now)
    assert brief.weekday
