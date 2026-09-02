"""Shared fixtures. Every test runs against a real SQLite database.

An in-memory schema built from the same models the app uses catches the class
of bug a mocked session never will -- a wrong constraint, a bad cascade, a
column that does not exist.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("HUDDLE_DATABASE_URL", "sqlite://")
os.environ.setdefault("HUDDLE_SESSION_SECRET", "test-secret")
os.environ.setdefault("HUDDLE_YOTO_CLIENT_ID", "test-client")

from huddle.config import get_settings, reset_settings  # noqa: E402
from huddle.database.models import (  # noqa: E402
    Athlete,
    Base,
    Family,
    FavoriteTeam,
    Game,
    NewsStory,
    Team,
)


@pytest.fixture
def settings():
    reset_settings()
    yield get_settings()
    reset_settings()


@pytest.fixture
def db() -> Session:
    # StaticPool keeps one connection for the whole in-memory database, and
    # check_same_thread lets the TestClient's worker thread reach it. Without
    # both, a web test dies on "SQLite objects created in a thread...".
    engine = create_engine(
        "sqlite://",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def now() -> datetime:
    # A Tuesday, so weekday-conditional segments behave predictably.
    return datetime(2026, 9, 1, 11, 0, tzinfo=UTC)


@pytest.fixture
def family(db: Session) -> Family:
    row = Family(
        yoto_user_id="auth0|test",
        display_name="Test Family",
        timezone="America/New_York",
        publish_hour=6,
        target_minutes=5,
        listener_age=8,
    )
    db.add(row)
    db.flush()
    return row


@pytest.fixture
def braves(db: Session) -> Team:
    row = Team(
        league_key="mlb", external_id="15", display_name="Atlanta Braves",
        short_name="Braves", location="Atlanta", abbreviation="ATL",
        search_name="atlanta braves atl", record_summary="82-57",
        standing_summary="1st in NL East",
    )
    db.add(row)
    db.flush()
    return row


@pytest.fixture
def liverpool(db: Session) -> Team:
    row = Team(
        league_key="epl", external_id="364", display_name="Liverpool",
        short_name="Liverpool", location="Liverpool", abbreviation="LIV",
        search_name="liverpool liv", record_summary="1-1-1",
        standing_summary="8th in Premier League",
    )
    db.add(row)
    db.flush()
    return row


@pytest.fixture
def followed(db: Session, family: Family, braves: Team) -> Team:
    db.add(FavoriteTeam(family_id=family.id, team_id=braves.id, sort_order=0))
    db.flush()
    return braves


def add_game(
    db: Session, team: Team, *, start_at: datetime, state: str = "post",
    home: bool = False, home_score: int | None = None, away_score: int | None = None,
    opponent: str = "Washington Nationals", broadcast: str | None = None,
) -> Game:
    game = Game(
        league_key=team.league_key,
        external_id=f"g-{start_at.isoformat()}-{team.external_id}",
        name=f"{team.display_name} vs {opponent}",
        start_at=start_at,
        state=state,
        completed=state == "post",
        home_team_id=team.id if home else None,
        away_team_id=None if home else team.id,
        home_score=home_score,
        away_score=away_score,
        broadcast=broadcast,
        extra={
            "home_team_name": team.display_name if home else opponent,
            "away_team_name": opponent if home else team.display_name,
        },
    )
    db.add(game)
    db.flush()
    return game


def add_story(
    db: Session, *, league_key: str = "mlb", headline: str, summary: str = "",
    verdict: str = "allow", published_at: datetime | None = None, caution: bool = False,
) -> NewsStory:
    story = NewsStory(
        league_key=league_key,
        external_id=headline[:100],
        headline=headline,
        summary=summary or headline,
        story_type="recap",
        published_at=published_at or datetime.now(UTC) - timedelta(hours=2),
        categories=[league_key.upper()],
        team_external_ids=[],
        safety_verdict=verdict,
        caution=caution,
    )
    db.add(story)
    db.flush()
    return story


def add_athlete(
    db: Session, team: Team, *, name: str, month: int, day: int, year: int = 1998
) -> Athlete:
    from datetime import date

    athlete = Athlete(
        league_key=team.league_key,
        external_id=f"a-{name}",
        team_id=team.id,
        full_name=name,
        position="Outfielder",
        birth_date=date(year, month, day),
        birth_month=month,
        birth_day=day,
        birth_place="Atlanta, GA",
        active=True,
    )
    db.add(athlete)
    db.flush()
    return athlete
