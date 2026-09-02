"""Assembling one family's brief for today's show.

The brief is the boundary between "what is true" and "how it is said". Every
fact the episode may state is gathered here, sourced and keyed; the writer
downstream chooses the words, and the narration model may only rephrase them.

Nothing here invents a value. A team with no result yet gets ``None`` and the
writer says so out loud, because a child who is told the score was nothing to
nothing when the game has not started has been misled.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from huddle.catalog import get_league
from huddle.content import Factoid, Moment, factoid_for, moment_for
from huddle.database.models import Athlete, FavoriteTeam, Game, NewsStory, Team
from huddle.domain.enums import GameState, ShowSegment
from huddle.domain.freshness import (
    FreshnessBlock,
    evaluate_freshness,
    infer_season_phase,
)
from huddle.ingestion.runner import collect_freshness
from huddle.providers.espn import ATTRIBUTION as ESPN_ATTRIBUTION

logger = logging.getLogger(__name__)

#: A result older than this is not "last night's game" any more.
RECENT_RESULT_HOURS = 42
#: How far ahead "upcoming" reaches for a team's next game.
UPCOMING_DAYS = 8
#: Stories older than this are not today's news.
STORY_WINDOW_HOURS = 30

#: An upper bound, not a target. The writer's character budget is what
#: actually decides how many run, so a slow news day simply produces fewer.
MAX_TODAY_STORIES = 8
MAX_BIRTHDAYS = 3
MAX_WEEKEND_GAMES = 4


@dataclass
class GameLine:
    """One game, reduced to what a host would actually say about it."""

    league_key: str
    league_spoken: str
    home_name: str | None
    away_name: str | None
    home_score: int | None
    away_score: int | None
    state: str
    start_at: str | None = None
    #: Local weekday and time in the family's timezone, already worded.
    when: str | None = None
    venue: str | None = None
    broadcast: str | None = None
    note: str | None = None

    @property
    def is_final(self) -> bool:
        return self.state == GameState.FINAL

    @property
    def has_score(self) -> bool:
        return self.home_score is not None and self.away_score is not None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass
class TeamUpdate:
    team_id: str
    name: str
    short_name: str
    nickname: str | None
    league_key: str
    league_spoken: str
    record_summary: str | None = None
    standing_summary: str | None = None
    last_game: GameLine | None = None
    next_game: GameLine | None = None
    #: One safe headline about this team, if there is one worth telling.
    story: str | None = None
    #: The "one thing to know" -- a single sentence of context, chosen from
    #: whichever fact today is most interesting rather than a fixed field.
    one_thing: str | None = None

    @property
    def spoken_name(self) -> str:
        """What the host calls them. A family's own nickname wins."""
        return self.nickname or self.short_name

    def as_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["last_game"] = self.last_game.as_dict() if self.last_game else None
        payload["next_game"] = self.next_game.as_dict() if self.next_game else None
        return payload


@dataclass
class StoryLine:
    league_key: str
    league_spoken: str
    headline: str
    summary: str | None
    shape: str | None = None
    caution: bool = False

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass
class BirthdayLine:
    name: str
    team_name: str | None
    league_spoken: str
    position: str | None
    age: int | None
    birth_place: str | None = None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass
class ShowBrief:
    family_id: str
    show_date: date
    generated_at: datetime
    weekday: str
    teams: list[TeamUpdate] = field(default_factory=list)
    today_stories: list[StoryLine] = field(default_factory=list)
    birthdays: list[BirthdayLine] = field(default_factory=list)
    moment: Moment | None = None
    factoid: Factoid | None = None
    weekend_games: list[GameLine] = field(default_factory=list)
    freshness: FreshnessBlock | None = None
    blocked_stories: list[dict] = field(default_factory=list)
    attribution: list[str] = field(default_factory=lambda: [ESPN_ATTRIBUTION])

    @property
    def has_any_sport(self) -> bool:
        """Whether anything live happened. On a completely quiet day the show
        still runs -- the curated segments carry it -- but the writer opens
        differently, because "big night of action!" over nothing is a lie."""
        return bool(self.teams or self.today_stories or self.weekend_games)

    def as_dict(self) -> dict[str, object]:
        return {
            "family_id": self.family_id,
            "show_date": self.show_date.isoformat(),
            "generated_at": self.generated_at.isoformat(),
            "weekday": self.weekday,
            "teams": [t.as_dict() for t in self.teams],
            "today_stories": [s.as_dict() for s in self.today_stories],
            "birthdays": [b.as_dict() for b in self.birthdays],
            "moment": asdict(self.moment) if self.moment else None,
            "factoid": asdict(self.factoid) if self.factoid else None,
            "weekend_games": [g.as_dict() for g in self.weekend_games],
            "freshness": self.freshness.as_dict() if self.freshness else None,
            "blocked_stories": list(self.blocked_stories),
            "attribution": list(self.attribution),
        }

    def content_hash(self) -> str:
        """Hash of everything a listener would hear.

        Excludes timestamps and freshness so two mornings with identical
        content hash identically and the second can be skipped instead of
        republishing the same show.
        """
        payload = {
            "teams": [
                (t.name, t.record_summary, t.story,
                 t.last_game.as_dict() if t.last_game else None,
                 t.next_game.as_dict() if t.next_game else None)
                for t in self.teams
            ],
            "stories": [s.headline for s in self.today_stories],
            "birthdays": [b.name for b in self.birthdays],
            "moment": self.moment.title if self.moment else None,
            "factoid": self.factoid.question if self.factoid else None,
            "weekend": [g.as_dict() for g in self.weekend_games],
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode()
        ).hexdigest()


def build_brief(
    session: Session,
    family,
    *,
    show_date: date | None = None,
    now: datetime | None = None,
) -> ShowBrief:
    """Gather everything today's show is allowed to say."""
    now = now or datetime.now(UTC)
    tz = _zone(family.timezone)
    local_now = now.astimezone(tz)
    show_date = show_date or local_now.date()

    favourites = _favourite_teams(session, family)
    segments = set(family.enabled_segments)

    teams = (
        _team_updates(session, favourites, now, tz)
        if ShowSegment.YOUR_TEAMS in segments
        else []
    )
    league_keys = sorted({team.league_key for _, team in favourites})

    stories = (
        _today_stories(session, league_keys, now, exclude=_used_headlines(teams))
        if ShowSegment.TODAY_IN_SPORTS in segments
        else []
    )
    birthdays = (
        _birthdays(session, favourites, show_date)
        if ShowSegment.BIRTHDAY_CLUB in segments
        else []
    )
    weekend = (
        _weekend_games(session, favourites, now, tz)
        if ShowSegment.WEEKEND_EDITION in segments
        else []
    )

    phase = infer_season_phase(now, has_games_today=_games_today(teams, local_now))
    freshness, _ = evaluate_freshness(
        collect_freshness(session), section="show", phase=phase, now=now
    )

    return ShowBrief(
        family_id=family.id,
        show_date=show_date,
        generated_at=now,
        weekday=local_now.strftime("%A"),
        teams=teams,
        today_stories=stories,
        birthdays=birthdays,
        moment=moment_for(show_date) if ShowSegment.ON_THIS_DAY in segments else None,
        factoid=factoid_for(show_date) if ShowSegment.ROOKIE_FACTOID in segments else None,
        weekend_games=weekend,
        freshness=freshness,
        blocked_stories=_blocked_today(session, league_keys, now),
    )


# -- favourites ------------------------------------------------------------
def _favourite_teams(session: Session, family) -> list[tuple[FavoriteTeam, Team]]:
    rows = list(
        session.scalars(
            select(FavoriteTeam)
            .where(FavoriteTeam.family_id == family.id)
            .order_by(FavoriteTeam.sort_order, FavoriteTeam.created_at)
        )
    )
    if not rows:
        return []
    teams = {
        team.id: team
        for team in session.scalars(
            select(Team).where(Team.id.in_([r.team_id for r in rows]))
        )
    }
    return [(row, teams[row.team_id]) for row in rows if row.team_id in teams]


# -- your teams ------------------------------------------------------------
def _team_updates(session, favourites, now: datetime, tz) -> list[TeamUpdate]:
    updates: list[TeamUpdate] = []
    for favourite, team in favourites:
        spec = get_league(team.league_key)
        games = _team_games(session, team, now)

        update = TeamUpdate(
            team_id=team.id,
            name=team.display_name,
            short_name=team.short_name,
            nickname=favourite.nickname,
            league_key=team.league_key,
            league_spoken=spec.spoken_name,
            record_summary=team.record_summary,
            standing_summary=team.standing_summary,
            last_game=_game_line(games.get("last"), spec, tz),
            next_game=_game_line(games.get("next"), spec, tz),
        )
        update.story = _team_story(session, team, now)
        update.one_thing = _one_thing(update)
        updates.append(update)
    return updates


def _team_games(session: Session, team: Team, now: datetime) -> dict[str, Game | None]:
    """The team's most recent finished game and its next scheduled one."""
    window_start = now - timedelta(hours=RECENT_RESULT_HOURS)
    window_end = now + timedelta(days=UPCOMING_DAYS)

    games = list(
        session.scalars(
            select(Game).where(
                Game.league_key == team.league_key,
                (Game.home_team_id == team.id) | (Game.away_team_id == team.id),
                Game.start_at.is_not(None),
                Game.start_at >= window_start,
                Game.start_at <= window_end,
            ).order_by(Game.start_at)
        )
    )
    finished = [g for g in games if g.state == GameState.FINAL and _aware(g.start_at) <= now]
    upcoming = [g for g in games if g.state != GameState.FINAL and _aware(g.start_at) >= now]
    return {
        "last": finished[-1] if finished else None,
        "next": upcoming[0] if upcoming else None,
    }


def _game_line(game: Game | None, spec, tz) -> GameLine | None:
    if game is None:
        return None
    extra = game.extra or {}
    start = _aware(game.start_at) if game.start_at else None
    notes = [n for n in (extra.get("notes") or []) if n]
    return GameLine(
        league_key=game.league_key,
        league_spoken=spec.spoken_name,
        home_name=extra.get("home_team_name"),
        away_name=extra.get("away_team_name"),
        home_score=game.home_score,
        away_score=game.away_score,
        state=game.state,
        start_at=start.isoformat() if start else None,
        when=_spoken_when(start, tz) if start else None,
        venue=game.venue,
        broadcast=game.broadcast,
        note=notes[0] if notes else None,
    )


def _spoken_when(start: datetime, tz) -> str:
    """A time a child can act on: "tonight", "Saturday morning"."""
    local = start.astimezone(tz)
    today = datetime.now(tz).date()
    delta = (local.date() - today).days
    hour = local.hour
    part = "morning" if hour < 12 else "afternoon" if hour < 17 else "evening"
    clock = local.strftime("%-I:%M" if local.minute else "%-I").lstrip("0")
    suffix = "in the morning" if hour < 12 else "in the afternoon" if hour < 17 else "at night"

    if delta == 0:
        return f"today at {clock} {suffix}" if hour >= 12 else f"this morning at {clock}"
    if delta == 1:
        return f"tomorrow {part} at {clock}"
    if 2 <= delta <= 6:
        return f"{local.strftime('%A')} {part} at {clock}"
    return f"{local.strftime('%A, %B')} {local.day}"


def _team_story(session: Session, team: Team, now: datetime) -> str | None:
    """One safe headline mentioning this team, preferring the gentle ones."""
    cutoff = now - timedelta(hours=STORY_WINDOW_HOURS)
    stories = list(
        session.scalars(
            select(NewsStory).where(
                NewsStory.league_key == team.league_key,
                NewsStory.safety_verdict == "allow",
                NewsStory.published_at.is_not(None),
                NewsStory.published_at >= cutoff,
            ).order_by(NewsStory.published_at.desc())
        )
    )
    name_parts = {p.lower() for p in (team.short_name, team.display_name, team.location) if p}
    for story in sorted(stories, key=lambda s: (s.caution, )):
        blob = f"{story.headline} {' '.join(str(c) for c in story.categories or [])}".lower()
        if team.external_id in (story.team_external_ids or []) or any(
            part in blob for part in name_parts
        ):
            return story.headline
    return None


def _one_thing(update: TeamUpdate) -> str | None:
    """The single most interesting true thing about this team right now.

    Ordered by what a child would actually care about: how the game went, then
    when the next one is, then where they sit in the table.
    """
    last, nxt = update.last_game, update.next_game
    if last is not None and last.has_score:
        margin = abs((last.home_score or 0) - (last.away_score or 0))
        if margin == 0:
            return "That one ended level."
        if margin == 1:
            return "They decided it by a single point."
        if margin >= 20:
            return "That was not close for long."
    if update.standing_summary:
        return f"Right now they sit {say_standing(update.standing_summary)}."
    if nxt is not None and nxt.broadcast:
        return f"You can watch on {nxt.broadcast}."
    return None


#: League abbreviations a synthesiser reads as words unless they are spaced.
_SPOKEN_ABBREVIATIONS = {
    "NL": "N L", "AL": "A L", "NFC": "N F C", "AFC": "A F C",
    "MLS": "M L S", "NBA": "N B A", "WNBA": "W N B A", "NHL": "N H L",
}


def say_standing(summary: str) -> str:
    """"1st in NL East" -> "1st in the N L East".

    Casing is preserved deliberately: lowercasing the whole string turns
    "NL East" into "nl east", which a voice reads as a word.
    """
    words = [_SPOKEN_ABBREVIATIONS.get(word, word) for word in summary.split()]
    spoken = " ".join(words)
    # "2nd in Western Conference" reads better with an article.
    for preposition in (" in ",):
        if preposition in spoken and " the " not in spoken:
            head, _, tail = spoken.partition(preposition)
            spoken = f"{head}{preposition}the {tail}"
    return spoken


def _used_headlines(teams: list[TeamUpdate]) -> set[str]:
    return {t.story for t in teams if t.story}


# -- today in sports -------------------------------------------------------
def _today_stories(
    session: Session, league_keys: list[str], now: datetime, exclude: set[str]
) -> list[StoryLine]:
    """The biggest safe stories, across the leagues this family follows.

    Falls back to every league when a family follows none -- a brand-new
    account should still get a real show on day one.
    """
    cutoff = now - timedelta(hours=STORY_WINDOW_HOURS)
    stmt = select(NewsStory).where(
        NewsStory.safety_verdict == "allow",
        NewsStory.published_at.is_not(None),
        NewsStory.published_at >= cutoff,
    )
    if league_keys:
        stmt = stmt.where(NewsStory.league_key.in_(league_keys))

    stories = list(session.scalars(stmt.order_by(NewsStory.published_at.desc())))
    # Gentle stories first, then newest. A "caution" story can still air, but
    # it should never lead the segment.
    stories.sort(key=lambda s: (s.caution, -_epoch(s.published_at)))

    lines: list[StoryLine] = []
    per_league: dict[str, int] = {}
    for story in stories:
        if story.headline in exclude:
            continue
        # Spread across leagues so a busy baseball night does not eat the whole
        # segment, but allow a second story per league once every followed
        # league has had a turn.
        used = per_league.get(story.league_key, 0)
        if used >= 1 and len(per_league) < len(league_keys or [1]):
            continue
        if used >= 2:
            continue
        per_league[story.league_key] = used + 1
        lines.append(
            StoryLine(
                league_key=story.league_key,
                league_spoken=get_league(story.league_key).spoken_name,
                headline=story.headline,
                summary=story.summary,
                caution=story.caution,
            )
        )
        if len(lines) >= MAX_TODAY_STORIES:
            break
    return lines


def _blocked_today(session: Session, league_keys: list[str], now: datetime) -> list[dict]:
    """What the safety filter removed, for the parent dashboard."""
    cutoff = now - timedelta(hours=STORY_WINDOW_HOURS)
    stmt = select(NewsStory).where(
        NewsStory.safety_verdict == "block",
        NewsStory.retrieved_at >= cutoff,
    )
    if league_keys:
        stmt = stmt.where(NewsStory.league_key.in_(league_keys))
    return [
        {
            "league": story.league_key,
            "headline": story.headline,
            "reason": story.safety_category,
            "matched": story.safety_matched,
        }
        for story in session.scalars(stmt.order_by(NewsStory.retrieved_at.desc()).limit(40))
    ]


# -- birthday club ---------------------------------------------------------
def _birthdays(session: Session, favourites, show_date: date) -> list[BirthdayLine]:
    """Players on followed teams with a birthday today.

    Deliberately scoped to followed teams: a child cares that someone on *their*
    team shares a birthday, and a league-wide list would be a roll call.
    """
    team_ids = [team.id for _, team in favourites]
    if not team_ids:
        return []

    athletes = list(
        session.scalars(
            select(Athlete).where(
                Athlete.team_id.in_(team_ids),
                Athlete.birth_month == show_date.month,
                Athlete.birth_day == show_date.day,
                Athlete.active.is_(True),
            )
        )
    )
    teams = {team.id: team for _, team in favourites}

    lines: list[BirthdayLine] = []
    for athlete in athletes[:MAX_BIRTHDAYS]:
        team = teams.get(athlete.team_id)
        age = None
        if athlete.birth_date:
            age = show_date.year - athlete.birth_date.year
        lines.append(
            BirthdayLine(
                name=athlete.full_name,
                team_name=team.short_name if team else None,
                league_spoken=get_league(athlete.league_key).spoken_name,
                position=athlete.position,
                age=age,
                birth_place=athlete.birth_place,
            )
        )
    return lines


# -- weekend edition -------------------------------------------------------
def _weekend_games(session, favourites, now: datetime, tz) -> list[GameLine]:
    """Followed teams playing between now and Sunday night."""
    team_ids = [team.id for _, team in favourites]
    if not team_ids:
        return []

    local = now.astimezone(tz)
    # Monday is 0; reach forward to the end of Sunday.
    days_to_sunday = 6 - local.weekday()
    window_end = now + timedelta(days=days_to_sunday + 1)

    games = list(
        session.scalars(
            select(Game).where(
                (Game.home_team_id.in_(team_ids)) | (Game.away_team_id.in_(team_ids)),
                Game.start_at.is_not(None),
                Game.start_at >= now,
                Game.start_at <= window_end,
                Game.state != GameState.FINAL,
            ).order_by(Game.start_at)
        )
    )
    lines = []
    for game in games[:MAX_WEEKEND_GAMES]:
        lines.append(_game_line(game, get_league(game.league_key), tz))
    return [line for line in lines if line]


# -- helpers ---------------------------------------------------------------
def _games_today(teams: list[TeamUpdate], local_now: datetime) -> bool:
    for team in teams:
        for game in (team.last_game, team.next_game):
            if game and game.start_at:
                start = datetime.fromisoformat(game.start_at)
                if start.astimezone(local_now.tzinfo).date() == local_now.date():
                    return True
    return False


def _zone(name: str | None):
    try:
        return ZoneInfo(name or "America/New_York")
    except Exception:  # noqa: BLE001 - a bad timezone must not stop a show
        logger.warning("unknown timezone %r; using UTC", name)
        return UTC


def _aware(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _epoch(value: datetime | None) -> float:
    return _aware(value).timestamp() if value else 0.0
