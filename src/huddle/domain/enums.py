"""Canonical vocabularies for the daily sportscast."""

from __future__ import annotations

from enum import StrEnum


class ShowSegment(StrEnum):
    """Segments of the daily show, in broadcast order."""

    YOUR_TEAMS = "your_teams"
    TODAY_IN_SPORTS = "today_in_sports"
    BIRTHDAY_CLUB = "birthday_club"
    ON_THIS_DAY = "on_this_day"
    ROOKIE_FACTOID = "rookie_factoid"
    WEEKEND_EDITION = "weekend_edition"


SEGMENT_ORDER: tuple[str, ...] = (
    ShowSegment.YOUR_TEAMS,
    ShowSegment.TODAY_IN_SPORTS,
    ShowSegment.BIRTHDAY_CLUB,
    ShowSegment.ON_THIS_DAY,
    ShowSegment.ROOKIE_FACTOID,
    ShowSegment.WEEKEND_EDITION,
)

SEGMENT_TITLES: dict[str, str] = {
    ShowSegment.YOUR_TEAMS: "Your Teams",
    ShowSegment.TODAY_IN_SPORTS: "Today in Sports",
    ShowSegment.BIRTHDAY_CLUB: "Birthday Club",
    ShowSegment.ON_THIS_DAY: "On This Day",
    ShowSegment.ROOKIE_FACTOID: "Rookie Factoid",
    ShowSegment.WEEKEND_EDITION: "Weekend Edition",
}

#: Segments that only earn their place some days. Everything else runs daily.
#: Weekend Edition previews the weekend, so it airs as the weekend arrives.
CONDITIONAL_SEGMENTS: dict[str, tuple[int, ...]] = {
    #: Monday is 0. Thursday, Friday, Saturday.
    ShowSegment.WEEKEND_EDITION: (3, 4, 5),
}


class Host(StrEnum):
    """The two voices. Consistency is the point -- a child should recognise
    them the way they recognise a favourite storybook narrator."""

    NOVA = "nova"
    RAE = "rae"


#: How each host is introduced and what they are for. Coach Nova carries the
#: facts and the explanations; Rookie Rae carries the energy and asks the
#: question a listening kid is already thinking.
HOST_PROFILES: dict[str, dict[str, str]] = {
    Host.NOVA: {
        "name": "Coach Nova",
        "role": "the calm one who explains what things mean",
    },
    Host.RAE: {
        "name": "Rookie Rae",
        "role": "the excited one who asks the questions kids ask",
    },
}


class GameState(StrEnum):
    SCHEDULED = "pre"
    IN_PROGRESS = "in"
    FINAL = "post"


class ShowStatus(StrEnum):
    PENDING = "pending"
    WRITING = "writing"
    SYNTHESIZING = "synthesizing"
    PUBLISHED = "published"
    FAILED = "failed"
    SKIPPED = "skipped"


class FreshnessClass(StrEnum):
    """How stale an input may be before the show stops trusting it."""

    STATIC = "static"   # team names, league structure
    SLOW = "slow"       # rosters, athlete birthdays
    DAILY = "daily"     # standings, schedules
    RAPID = "rapid"     # scores, news
    LIVE = "live"       # in-progress games


class SeasonPhase(StrEnum):
    OFFSEASON = "offseason"
    REGULAR_SEASON = "regular_season"
    GAME_DAY = "game_day"
    POSTSEASON = "postseason"


class Capability(StrEnum):
    """What an adapter can serve. The show asks for a capability, never for a
    named provider, so a fixture source can stand in for a live one."""

    LEAGUE_TEAMS = "league_teams"
    SCOREBOARD = "scoreboard"
    TEAM_DETAIL = "team_detail"
    TEAM_ROSTER = "team_roster"
    LEAGUE_NEWS = "league_news"


class SafetyVerdict(StrEnum):
    ALLOW = "allow"
    BLOCK = "block"


#: Speaking rate used to keep the show inside its target running time. Measured
#: against typical synthesised narration, which is slower than casual speech.
CHARS_PER_MINUTE = 850
