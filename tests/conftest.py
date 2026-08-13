from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from draftgpt.config import Settings
from draftgpt.database.models import Base
from draftgpt.database.seed import SEASON, build_fixture_files
from draftgpt.database.session import get_engine


@pytest.fixture()
def engine():
    """In-memory SQLite per test. Schema is created from the same metadata the
    migrations are generated from, so drift shows up as a test failure."""
    engine = get_engine("sqlite://")
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture()
def session(engine) -> Session:
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with factory() as session:
        yield session


@pytest.fixture(scope="session")
def fixtures_dir(tmp_path_factory) -> Path:
    target = tmp_path_factory.mktemp("fixtures")
    build_fixture_files(target)
    return target


@pytest.fixture()
def settings(fixtures_dir) -> Settings:
    return Settings(
        database_url="sqlite://",
        season=SEASON,
        league_provider="fixture_league",
        fixtures_dir=fixtures_dir,
    )


@pytest.fixture()
def seeded_league(session, settings):
    """A fully synced fixture league, ready for command-level tests."""
    from draftgpt.database.seed import BYE_TEAMS
    from draftgpt.evaluation.scoring import ScoringRules
    from draftgpt.ingestion.league_sync import sync_league
    from draftgpt.ingestion.projections_sync import sync_projections
    from draftgpt.ingestion.registry_sync import (
        ensure_nfl_teams,
        sync_players_from_league_provider,
    )
    from draftgpt.ingestion.runner import sync_provider_registry
    from draftgpt.providers.fixture import FixtureLeagueProvider, FixtureProjectionProvider

    league_provider = FixtureLeagueProvider(settings)
    projection_provider = FixtureProjectionProvider(settings)

    sync_provider_registry(session, [league_provider.spec, projection_provider.spec])
    teams = ensure_nfl_teams(session)
    for abbreviation, bye_week in BYE_TEAMS.items():
        if abbreviation in teams:
            teams[abbreviation].bye_week_by_season = {str(SEASON): bye_week}

    sync_players_from_league_provider(session, league_provider, SEASON)
    report = sync_league(session, league_provider, SEASON, owner_team_external_id="1")

    from draftgpt.database.models import LeagueRules

    rules = session.query(LeagueRules).order_by(LeagueRules.version.desc()).first()
    scoring = ScoringRules.from_config(rules.scoring, f"{rules.id}:v{rules.version}")
    sync_projections(session, projection_provider, SEASON, week=1, scope="week", rules=scoring)
    session.commit()
    return report
