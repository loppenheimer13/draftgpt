"""The league catalogue.

One table drives everything a parent can follow. Each entry maps a stable
Huddle key onto the sport and league path the data provider uses, plus the
words the show says out loud -- because "N F L" and "the Premier League" are
both spoken forms a text-to-speech voice will otherwise mangle.

Adding a league is a row here plus a fixture; nothing downstream needs to know
which sport it is.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class SportGroup(StrEnum):
    """How leagues are grouped in the parent's picker."""

    PRO_US = "pro_us"
    COLLEGE = "college"
    SOCCER = "soccer"


@dataclass(frozen=True)
class LeagueSpec:
    key: str
    #: Provider path components, e.g. ("basketball", "nba").
    sport: str
    path: str
    display_name: str
    #: What a host says, article included. Letters are spaced because a
    #: synthesiser reads "NFL" as a word ("nuh-full") otherwise, and the
    #: article is baked in so "From the N F L" and "From Major League
    #: Baseball" both come out as English.
    spoken_name: str
    group: SportGroup
    #: A one-line, kid-facing description used in the picker and on first mention.
    blurb: str
    #: Months (1-12) the league is normally active. Used to decide whether an
    #: empty scoreboard means "offseason" or "something is broken".
    season_months: tuple[int, ...] = tuple(range(1, 13))
    #: College and cup competitions have hundreds of teams; the picker searches
    #: rather than listing them all.
    large_roster: bool = False

    @property
    def provider_path(self) -> str:
        return f"{self.sport}/{self.path}"

    def in_season(self, month: int) -> bool:
        return month in self.season_months


LEAGUES: tuple[LeagueSpec, ...] = (
    LeagueSpec(
        key="nfl", sport="football", path="nfl",
        display_name="NFL", spoken_name="the N F L",
        group=SportGroup.PRO_US,
        blurb="American football's biggest league, played mostly on Sundays.",
        season_months=(1, 2, 9, 10, 11, 12),
    ),
    LeagueSpec(
        key="nba", sport="basketball", path="nba",
        display_name="NBA", spoken_name="the N B A",
        group=SportGroup.PRO_US,
        blurb="The top basketball league in the world.",
        season_months=(1, 2, 3, 4, 5, 6, 10, 11, 12),
    ),
    LeagueSpec(
        key="wnba", sport="basketball", path="wnba",
        display_name="WNBA", spoken_name="the W N B A",
        group=SportGroup.PRO_US,
        blurb="The premier women's basketball league, played in the summer.",
        season_months=(5, 6, 7, 8, 9, 10),
    ),
    LeagueSpec(
        key="mlb", sport="baseball", path="mlb",
        display_name="MLB", spoken_name="Major League Baseball",
        group=SportGroup.PRO_US,
        blurb="Baseball's big leagues, with games almost every day all summer.",
        season_months=(3, 4, 5, 6, 7, 8, 9, 10),
    ),
    LeagueSpec(
        key="nhl", sport="hockey", path="nhl",
        display_name="NHL", spoken_name="the N H L",
        group=SportGroup.PRO_US,
        blurb="Professional ice hockey, played on frozen rinks.",
        season_months=(1, 2, 3, 4, 5, 6, 10, 11, 12),
    ),
    LeagueSpec(
        key="ncaaf", sport="football", path="college-football",
        display_name="College Football", spoken_name="college football",
        group=SportGroup.COLLEGE,
        blurb="University teams playing football on Saturdays.",
        season_months=(1, 8, 9, 10, 11, 12), large_roster=True,
    ),
    LeagueSpec(
        key="ncaam", sport="basketball", path="mens-college-basketball",
        display_name="Men's College Basketball", spoken_name="men's college basketball",
        group=SportGroup.COLLEGE,
        blurb="University basketball, home of the March tournament.",
        season_months=(1, 2, 3, 4, 11, 12), large_roster=True,
    ),
    LeagueSpec(
        key="ncaaw", sport="basketball", path="womens-college-basketball",
        display_name="Women's College Basketball", spoken_name="women's college basketball",
        group=SportGroup.COLLEGE,
        blurb="University women's basketball, with its own March tournament.",
        season_months=(1, 2, 3, 4, 11, 12), large_roster=True,
    ),
    LeagueSpec(
        key="epl", sport="soccer", path="eng.1",
        display_name="Premier League", spoken_name="the Premier League",
        group=SportGroup.SOCCER,
        blurb="England's top football league.",
        season_months=(1, 2, 3, 4, 5, 8, 9, 10, 11, 12),
    ),
    LeagueSpec(
        key="mls", sport="soccer", path="usa.1",
        display_name="MLS", spoken_name="Major League Soccer",
        group=SportGroup.SOCCER,
        blurb="Major League Soccer, played across the United States and Canada.",
        season_months=(2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12),
    ),
    LeagueSpec(
        key="nwsl", sport="soccer", path="usa.nwsl",
        display_name="NWSL", spoken_name="the N W S L",
        group=SportGroup.SOCCER,
        blurb="The National Women's Soccer League.",
        season_months=(3, 4, 5, 6, 7, 8, 9, 10, 11),
    ),
    LeagueSpec(
        key="laliga", sport="soccer", path="esp.1",
        display_name="LaLiga", spoken_name="La Liga",
        group=SportGroup.SOCCER,
        blurb="Spain's top football league.",
        season_months=(1, 2, 3, 4, 5, 8, 9, 10, 11, 12),
    ),
    LeagueSpec(
        key="ucl", sport="soccer", path="uefa.champions",
        display_name="Champions League", spoken_name="the Champions League",
        group=SportGroup.SOCCER,
        blurb="Europe's best club teams playing each other midweek.",
        season_months=(1, 2, 3, 4, 5, 9, 10, 11, 12), large_roster=True,
    ),
    LeagueSpec(
        key="ligamx", sport="soccer", path="mex.1",
        display_name="Liga MX", spoken_name="Liga M X",
        group=SportGroup.SOCCER,
        blurb="Mexico's top football league.",
        season_months=(1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 12),
    ),
)

LEAGUES_BY_KEY: dict[str, LeagueSpec] = {spec.key: spec for spec in LEAGUES}


def get_league(key: str) -> LeagueSpec:
    try:
        return LEAGUES_BY_KEY[key]
    except KeyError as exc:
        raise KeyError(
            f"unknown league '{key}'; known: {', '.join(sorted(LEAGUES_BY_KEY))}"
        ) from exc


def leagues_in_group(group: SportGroup | str) -> list[LeagueSpec]:
    return [spec for spec in LEAGUES if spec.group == group]


def leagues_in_season(month: int) -> list[LeagueSpec]:
    return [spec for spec in LEAGUES if spec.in_season(month)]
