"""Recorded fixtures, for tests and offline demos.

Serves the same capabilities as the live adapter from JSON files under
``fixtures/``. Selected only when ``HUDDLE_USE_FIXTURES=true``, so a
misconfigured production run fails loudly instead of quietly narrating stale
recorded data as though it were this morning's.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from huddle.config import Settings
from huddle.domain.enums import Capability, FreshnessClass
from huddle.providers.base import (
    BaseProvider,
    FetchResult,
    ProviderSpec,
    ProviderUnavailable,
)
from huddle.providers.registry import register

PROVIDER_KEY = "fixture"

SPEC = ProviderSpec(
    key=PROVIDER_KEY,
    display_name="Recorded fixtures",
    categories=("teams", "scores", "news", "athletes"),
    capabilities=(
        Capability.LEAGUE_TEAMS,
        Capability.TEAM_DETAIL,
        Capability.TEAM_ROSTER,
        Capability.SCOREBOARD,
        Capability.LEAGUE_NEWS,
    ),
    freshness_class=FreshnessClass.STATIC,
    requires_auth=False,
    license_note="recorded from public endpoints for testing; never published",
    fallback="none -- fixtures are the fallback",
)


class FixtureProvider(BaseProvider):
    spec = SPEC

    def __init__(self, settings: Settings) -> None:
        self._dir = Path(settings.fixtures_dir)

    def _load(self, name: str) -> Any:
        path = self._dir / f"{name}.json"
        if not path.exists():
            raise ProviderUnavailable(PROVIDER_KEY, name, f"no fixture at {path}")
        return json.loads(path.read_text())

    def _result(self, records: list[dict[str, Any]]) -> FetchResult:
        return FetchResult(
            capability="",
            records=records,
            retrieved_at=datetime.now(UTC),
            notes=["recorded fixture data, not live"],
        )

    def fetch_league_teams(self, league: str, **_: Any) -> FetchResult:
        return self._result(self._load(f"teams_{league}"))

    def fetch_scoreboard(self, league: str, **_: Any) -> FetchResult:
        return self._result(self._load(f"scoreboard_{league}"))

    def fetch_league_news(self, league: str, **_: Any) -> FetchResult:
        return self._result(self._load(f"news_{league}"))

    def fetch_team_detail(self, league: str, team_external_id: str, **_: Any) -> FetchResult:
        records = self._load(f"team_detail_{league}")
        return self._result([r for r in records if r.get("external_id") == team_external_id])

    def fetch_team_roster(self, league: str, team_external_id: str, **_: Any) -> FetchResult:
        records = self._load(f"roster_{league}")
        return self._result(
            [r for r in records if r.get("team_external_id") == team_external_id]
        )


@register(PROVIDER_KEY)
def _build(settings: Settings) -> FixtureProvider:
    return FixtureProvider(settings)
