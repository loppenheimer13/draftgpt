"""Fixture-backed providers.

These exist for three reasons the brief calls for directly:

1. The vertical slice must run end to end before any credential or licensed
   feed is available.
2. Golden-scenario tests need a frozen, hand-authored league state.
3. A licensed projection source that cannot yet be integrated gets a legal
   stand-in behind the same interface, so swapping it in later is a config
   change rather than a rewrite.

Projections load from CSV so the owner can hand-import any source they are
entitled to use without writing code.
"""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from draftgpt.config import Settings
from draftgpt.domain.enums import Capability, FreshnessClass
from draftgpt.providers.base import (
    FetchResult,
    LeagueProvider,
    ProjectionProvider,
    ProviderError,
    ProviderSpec,
)
from draftgpt.providers.registry import register

FIXTURE_LEAGUE_SPEC = ProviderSpec(
    key="fixture_league",
    display_name="Fixture League (local JSON)",
    categories=("league", "registry"),
    capabilities=(
        Capability.LEAGUE_SETTINGS,
        Capability.LEAGUE_ROSTERS,
        Capability.LEAGUE_FREE_AGENTS,
        Capability.PLAYER_REGISTRY,
        Capability.LEAGUE_DRAFT,
    ),
    freshness_class=FreshnessClass.STATIC,
    requires_auth=False,
    license_note="Locally authored fixture data. No third-party terms apply.",
    fallback="none -- this is itself the fallback",
)

FIXTURE_PROJECTION_SPEC = ProviderSpec(
    key="fixture_projections",
    display_name="Fixture Projections (local CSV)",
    categories=("projection",),
    capabilities=(
        Capability.PROJECTIONS_WEEKLY,
        Capability.PROJECTIONS_SEASON,
        Capability.PROJECTIONS_ROS,
        Capability.ADP,
    ),
    freshness_class=FreshnessClass.SLOW,
    requires_auth=False,
    license_note=(
        "Locally authored or manually imported CSV. The owner is responsible for "
        "holding rights to any data they import here; nothing is fetched remotely."
    ),
    fallback="none -- this is itself the fallback",
)


def _read_json(path: Path) -> Any:
    if not path.exists():
        raise ProviderError("fixture", "load", f"fixture file not found: {path}")
    return json.loads(path.read_text())


class FixtureLeagueProvider(LeagueProvider):
    spec = FIXTURE_LEAGUE_SPEC

    def __init__(self, settings: Settings) -> None:
        self._dir = Path(settings.fixtures_dir)
        self._season = settings.season

    def _result(self, records: list[dict[str, Any]]) -> FetchResult:
        return FetchResult(capability="", records=records, retrieved_at=datetime.now(UTC))

    def fetch_league_settings(self, **_: Any) -> FetchResult:
        return self._result([_read_json(self._dir / "league_settings.json")])

    def fetch_league_rosters(self, **_: Any) -> FetchResult:
        return self._result(_read_json(self._dir / "league_rosters.json"))

    def fetch_player_registry(self, **_: Any) -> FetchResult:
        return self._result(_read_json(self._dir / "players.json"))

    def fetch_league_free_agents(self, **_: Any) -> FetchResult:
        players = _read_json(self._dir / "players.json")
        rosters = _read_json(self._dir / "league_rosters.json")
        rostered = {
            p["player_external_id"] for team in rosters for p in team.get("players", [])
        }
        return self._result([p for p in players if p["player_external_id"] not in rostered])

    def fetch_league_draft(self, **_: Any) -> FetchResult:
        path = self._dir / "draft.json"
        if not path.exists():
            return self._result([])
        return self._result([_read_json(path)])


class FixtureProjectionProvider(ProjectionProvider):
    spec = FIXTURE_PROJECTION_SPEC

    def __init__(self, settings: Settings) -> None:
        self._dir = Path(settings.fixtures_dir)
        self._season = settings.season

    def _load_csv(self, filename: str) -> list[dict[str, Any]]:
        path = self._dir / filename
        if not path.exists():
            raise ProviderError("fixture_projections", "load", f"missing {path}")
        with path.open(newline="") as handle:
            rows = list(csv.DictReader(handle))
        return [_coerce_projection_row(row) for row in rows]

    def fetch_projections_weekly(self, week: int, **_: Any) -> FetchResult:
        rows = [r for r in self._load_csv("projections_weekly.csv") if r["week"] == week]
        return FetchResult(
            capability="",
            records=rows,
            retrieved_at=datetime.now(UTC),
            effective_at=datetime.now(UTC),
        )

    def fetch_projections_season(self, **_: Any) -> FetchResult:
        return FetchResult(
            capability="",
            records=self._load_csv("projections_season.csv"),
            retrieved_at=datetime.now(UTC),
        )

    def fetch_adp(self, **_: Any) -> FetchResult:
        path = self._dir / "adp.csv"
        if not path.exists():
            return FetchResult(capability="", records=[], retrieved_at=datetime.now(UTC))
        with path.open(newline="") as handle:
            rows = list(csv.DictReader(handle))
        return FetchResult(
            capability="",
            records=[
                {
                    "player_external_id": row["player_external_id"],
                    "name": row.get("name"),
                    "position": row.get("position"),
                    "adp": float(row["adp"]),
                    "adp_stdev": _opt_float(row.get("adp_stdev")),
                }
                for row in rows
                if row.get("adp")
            ],
            retrieved_at=datetime.now(UTC),
        )

    def fetch_projections_ros(self, **_: Any) -> FetchResult:
        path = self._dir / "projections_ros.csv"
        if not path.exists():
            # Rest-of-season is derivable from season totals when not supplied.
            return FetchResult(capability="", records=[], retrieved_at=datetime.now(UTC))
        return FetchResult(
            capability="", records=self._load_csv("projections_ros.csv"),
            retrieved_at=datetime.now(UTC),
        )


#: Columns that are not stat keys.
_META_COLUMNS = {
    "player_external_id", "provider_player_id", "name", "position", "team",
    "week", "season", "scope", "floor_points", "ceiling_points", "stdev_points",
    "projected_points",
}


def _coerce_projection_row(row: dict[str, str]) -> dict[str, Any]:
    """CSV row -> normalized projection record.

    Any column that is not metadata is treated as a canonical stat key, so a new
    scoring category needs no code change -- just a new column.
    """
    stat_line = {
        key: float(value)
        for key, value in row.items()
        if key and key not in _META_COLUMNS and value not in (None, "", "NA")
    }
    return {
        "player_external_id": row.get("player_external_id") or row.get("provider_player_id"),
        "name": row.get("name"),
        "position": row.get("position"),
        "team": row.get("team"),
        "season": int(row["season"]) if row.get("season") else None,
        "week": int(row["week"]) if row.get("week") else None,
        "scope": row.get("scope") or ("week" if row.get("week") else "season"),
        "stat_line": stat_line,
        "floor_points": _opt_float(row.get("floor_points")),
        "ceiling_points": _opt_float(row.get("ceiling_points")),
        "stdev_points": _opt_float(row.get("stdev_points")),
        "projected_points": _opt_float(row.get("projected_points")),
    }


def _opt_float(value: str | None) -> float | None:
    if value in (None, "", "NA"):
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


# Registered last so a real provider always wins ties in the registry.
@register("fixture_league")
def _build_league(settings: Settings) -> FixtureLeagueProvider:
    return FixtureLeagueProvider(settings)


@register("fixture_projections")
def _build_projections(settings: Settings) -> FixtureProjectionProvider:
    return FixtureProjectionProvider(settings)
