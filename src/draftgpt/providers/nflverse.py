"""nflverse provider adapter (via ``nflreadpy``).

This one adapter serves the player registry, the cross-platform ID crosswalk,
schedule, injuries with practice participation, depth charts, usage, weather,
and betting context. It is free, stable, versioned, and legally clean.

Identity model
--------------
``gsis_id`` is nflverse's anchor key. ``load_rosters()`` carries the bridges we
need -- ``espn_id`` (our league platform), ``pfr_id`` (snap counts),
``sleeper_id``/``yahoo_id`` (future platforms) -- which makes the roster table
the hub of a hub-and-spoke crosswalk.

Measured coverage caveat: ``espn_id`` is present for ~84% of fantasy-relevant
players (2024 season). The remaining ~16% -- mostly rookies and practice-squad
callups -- still require the conservative name resolver, so that path is load
bearing, not vestigial.

Licensing
---------
nflverse data is CC-BY-4.0: attribution is required and is emitted in every
recommendation's provenance. ``load_ftn_charting`` is deliberately NOT exposed
here because FTN data is CC-BY-SA-4.0 and share-alike obligations should not
attach to this project by accident. ``load_ff_playerids``/``load_ff_rankings``
proxy DynastyProcess (GPL-3.0 repo) and are therefore optional supplements
rather than load-bearing inputs.
"""

from __future__ import annotations

import contextlib
import logging
from datetime import UTC, datetime, time, timedelta
from typing import Any

from draftgpt.config import Settings
from draftgpt.domain.enums import Capability, FreshnessClass
from draftgpt.providers.base import (
    ContextProvider,
    FetchResult,
    ProviderSpec,
    ProviderUnavailable,
)
from draftgpt.providers.registry import register

logger = logging.getLogger(__name__)

PROVIDER_KEY = "nflverse"

ATTRIBUTION = "Data from nflverse (https://nflverse.com), licensed CC-BY-4.0."

SPEC = ProviderSpec(
    key=PROVIDER_KEY,
    display_name="nflverse (nflreadpy)",
    categories=("registry", "context", "market"),
    capabilities=(
        Capability.PLAYER_REGISTRY,
        Capability.SCHEDULE,
        Capability.INJURIES,
        Capability.DEPTH_CHARTS,
        Capability.USAGE,
        Capability.WEATHER,
        Capability.BETTING_CONTEXT,
        Capability.RANKINGS,
    ),
    freshness_class=FreshnessClass.DAILY,
    requires_auth=False,
    rate_limit_per_min=None,
    raw_retention_days=None,
    license_note=(
        "nflverse data is CC-BY-4.0; attribution required and emitted in "
        "recommendation provenance. FTN charting (CC-BY-SA-4.0) is intentionally "
        "not exposed. ff_playerids/ff_rankings proxy DynastyProcess (GPL-3.0) and "
        "are optional supplements only."
    ),
    fallback="use last successful snapshot; degrade confidence and warn",
)

#: nflverse team abbreviations that differ from ESPN's.
TEAM_ALIASES: dict[str, str] = {"LA": "LAR", "OAK": "LV", "SD": "LAC", "STL": "LAR", "WAS": "WSH"}

#: nflverse practice/report status -> canonical designation.
REPORT_STATUS: dict[str, str] = {
    "Out": "O",
    "Doubtful": "D",
    "Questionable": "Q",
    "Injured Reserve": "IR",
}
PRACTICE_STATUS: dict[str, str] = {
    "Did Not Participate In Practice": "DNP",
    "Limited Participation in Practice": "LP",
    "Full Participation in Practice": "FP",
}

FANTASY_POSITIONS = ("QB", "RB", "WR", "TE", "K")


def _nfl() -> Any:
    try:
        import nflreadpy
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise ProviderUnavailable(
            PROVIDER_KEY,
            "import",
            "nflreadpy is not installed -- `pip install nflreadpy`",
        ) from exc
    return nflreadpy


def _norm_team(team: str | None) -> str | None:
    if not team:
        return None
    return TEAM_ALIASES.get(team, team)


def _rows(frame: Any) -> list[dict[str, Any]]:
    """Polars DataFrame -> list of dicts, without requiring polars at import."""
    if frame is None:
        return []
    if hasattr(frame, "to_dicts"):
        return frame.to_dicts()
    if hasattr(frame, "to_dict"):  # pandas fallback
        return frame.to_dict(orient="records")
    return list(frame)


class NflverseProvider(ContextProvider):
    spec = SPEC

    def __init__(self, settings: Settings) -> None:
        self._season = settings.season

    def health_check(self) -> tuple[bool, str]:
        try:
            _nfl()
        except ProviderUnavailable as exc:
            return False, str(exc)
        return True, "ok"

    def _result(self, records: list[dict[str, Any]], **kw: Any) -> FetchResult:
        notes = list(kw.pop("notes", []))
        notes.append(ATTRIBUTION)
        return FetchResult(
            capability="", records=records, retrieved_at=datetime.now(UTC), notes=notes, **kw
        )

    # -- player registry + ID crosswalk -----------------------------------
    def fetch_player_registry(
        self, season: int | None = None, fantasy_only: bool = True, **_: Any
    ) -> FetchResult:
        """Canonical players with every provider ID nflverse carries.

        This is what lets us link an ESPN roster entry to a canonical player by
        *identifier* rather than by name.
        """
        season = season or self._season
        frame = self._load_rosters(season)
        records: list[dict[str, Any]] = []
        missing_espn = 0

        for row in _rows(frame):
            position = row.get("position")
            if fantasy_only and position not in FANTASY_POSITIONS:
                continue
            gsis_id = row.get("gsis_id")
            if not gsis_id:
                continue
            provider_ids = {
                key: str(row[col])
                for key, col in (
                    ("espn", "espn_id"),
                    ("sleeper", "sleeper_id"),
                    ("yahoo", "yahoo_id"),
                    ("pfr", "pfr_id"),
                    ("sportradar", "sportradar_id"),
                    ("fantasy_data", "fantasy_data_id"),
                    ("pff", "pff_id"),
                    ("rotowire", "rotowire_id"),
                )
                if row.get(col) not in (None, "")
            }
            provider_ids["nflverse"] = str(gsis_id)
            if "espn" not in provider_ids:
                missing_espn += 1

            records.append(
                {
                    "external_id": str(gsis_id),
                    "gsis_id": str(gsis_id),
                    "name": row.get("full_name")
                    or f"{row.get('first_name', '')} {row.get('last_name', '')}".strip(),
                    "first_name": row.get("first_name"),
                    "last_name": row.get("last_name"),
                    "position": position,
                    "team": _norm_team(row.get("team")),
                    "jersey_number": _opt_int(row.get("jersey_number")),
                    "birth_date": row.get("birth_date"),
                    "status": (row.get("status") or "ACT"),
                    "years_exp": _opt_int(row.get("years_exp")),
                    "provider_ids": provider_ids,
                    "season": season,
                }
            )

        notes = []
        if records:
            pct = 100.0 * (len(records) - missing_espn) / len(records)
            notes.append(
                f"espn_id present for {len(records) - missing_espn}/{len(records)} "
                f"({pct:.1f}%) players; remainder resolve by name"
            )
        return self._result(records, notes=notes)

    def _load_rosters(self, season: int) -> Any:
        nfl = _nfl()
        try:
            return nfl.load_rosters(seasons=[season])
        except Exception as exc:  # noqa: BLE001
            # A season that has not started yet has no roster file published.
            logger.info("nflverse rosters unavailable for %s, falling back", season)
            try:
                return nfl.load_rosters(seasons=[season - 1])
            except Exception as inner:  # noqa: BLE001
                raise ProviderUnavailable(
                    PROVIDER_KEY, "player_registry", f"roster load failed: {exc}"
                ) from inner

    # -- schedule / weather / betting -------------------------------------
    def fetch_schedule(self, season: int | None = None, **_: Any) -> FetchResult:
        season = season or self._season
        nfl = _nfl()
        try:
            frame = nfl.load_schedules(seasons=[season])
        except Exception as exc:  # noqa: BLE001
            raise ProviderUnavailable(PROVIDER_KEY, "schedule", str(exc)) from exc

        records = []
        for row in _rows(frame):
            records.append(
                {
                    "external_id": row.get("game_id"),
                    "season": _opt_int(row.get("season")),
                    "week": _opt_int(row.get("week")),
                    "game_type": row.get("game_type"),
                    "home_team": _norm_team(row.get("home_team")),
                    "away_team": _norm_team(row.get("away_team")),
                    "kickoff_at": _kickoff(row.get("gameday"), row.get("gametime")),
                    "stadium": row.get("stadium"),
                    "roof": row.get("roof"),
                    "surface": row.get("surface"),
                    "is_dome": row.get("roof") in {"dome", "closed"},
                    "home_score": _opt_int(row.get("home_score")),
                    "away_score": _opt_int(row.get("away_score")),
                    "home_rest": _opt_int(row.get("home_rest")),
                    "away_rest": _opt_int(row.get("away_rest")),
                    "div_game": bool(row.get("div_game")),
                }
            )
        return self._result(records)

    def fetch_betting_context(self, season: int | None = None, **_: Any) -> FetchResult:
        """Spread and total -> implied team totals, the game-script prior.

        Note: nflverse carries *closing* lines for completed games and current
        lines for upcoming ones, so this is a slow-moving input, not a live
        market feed. Treated as `daily`, not `rapid`.
        """
        season = season or self._season
        nfl = _nfl()
        try:
            frame = nfl.load_schedules(seasons=[season])
        except Exception as exc:  # noqa: BLE001
            raise ProviderUnavailable(PROVIDER_KEY, "betting_context", str(exc)) from exc

        records = []
        for row in _rows(frame):
            total = _opt_float(row.get("total_line"))
            spread = _opt_float(row.get("spread_line"))
            if total is None and spread is None:
                continue
            home_implied = away_implied = None
            if total is not None and spread is not None:
                # spread_line is the home-team line (negative = home favored).
                home_implied = round(total / 2 + spread / 2, 2)
                away_implied = round(total / 2 - spread / 2, 2)
            records.append(
                {
                    "game_external_id": row.get("game_id"),
                    "week": _opt_int(row.get("week")),
                    "total": total,
                    "home_spread": spread,
                    "home_implied_total": home_implied,
                    "away_implied_total": away_implied,
                    "home_moneyline": _opt_int(row.get("home_moneyline")),
                    "away_moneyline": _opt_int(row.get("away_moneyline")),
                }
            )
        return self._result(records)

    def fetch_weather(self, season: int | None = None, **_: Any) -> FetchResult:
        """Temperature and wind from the schedule file.

        Caveat worth respecting: these are populated for played games and are
        often null for future outdoor games until close to kickoff, and always
        null for domes. Absence is not "good weather" -- callers must treat
        null as unknown.
        """
        season = season or self._season
        nfl = _nfl()
        try:
            frame = nfl.load_schedules(seasons=[season])
        except Exception as exc:  # noqa: BLE001
            raise ProviderUnavailable(PROVIDER_KEY, "weather", str(exc)) from exc

        records = []
        for row in _rows(frame):
            roof = row.get("roof")
            temp, wind = _opt_float(row.get("temp")), _opt_float(row.get("wind"))
            if temp is None and wind is None and roof not in {"dome", "closed"}:
                continue
            records.append(
                {
                    "game_external_id": row.get("game_id"),
                    "week": _opt_int(row.get("week")),
                    "temperature_f": temp,
                    "wind_mph": 0.0 if roof in {"dome", "closed"} else wind,
                    "conditions": roof,
                    "is_indoor": roof in {"dome", "closed"},
                }
            )
        return self._result(records)

    # -- injuries + practice participation ---------------------------------
    def fetch_injuries(self, season: int | None = None, week: int | None = None, **_: Any):
        """Game-status designation *and* practice participation.

        Practice participation is the leading indicator the brief calls for: a
        Wednesday DNP is a materially different signal from a Friday full
        practice, even when both carry a Questionable tag.
        """
        season = season or self._season
        nfl = _nfl()
        try:
            frame = nfl.load_injuries(seasons=[season])
        except Exception as exc:  # noqa: BLE001
            raise ProviderUnavailable(PROVIDER_KEY, "injuries", str(exc)) from exc

        records = []
        for row in _rows(frame):
            if week is not None and _opt_int(row.get("week")) != week:
                continue
            gsis_id = row.get("gsis_id")
            if not gsis_id:
                continue
            report = row.get("report_status")
            practice = row.get("practice_status")
            records.append(
                {
                    "player_external_id": str(gsis_id),
                    "season": _opt_int(row.get("season")),
                    "week": _opt_int(row.get("week")),
                    "event_type": "injury" if report else "practice",
                    "designation": REPORT_STATUS.get(report, report)
                    or PRACTICE_STATUS.get(practice, practice),
                    "practice_status": PRACTICE_STATUS.get(practice, practice),
                    "body_part": row.get("report_primary_injury")
                    or row.get("practice_primary_injury"),
                    "team": _norm_team(row.get("team")),
                    "detail": row.get("report_secondary_injury"),
                }
            )
        return self._result(records)

    # -- depth charts -------------------------------------------------------
    def fetch_depth_charts(self, season: int | None = None, week: int | None = None, **_: Any):
        season = season or self._season
        nfl = _nfl()
        try:
            frame = nfl.load_depth_charts(seasons=[season])
        except Exception as exc:  # noqa: BLE001
            raise ProviderUnavailable(PROVIDER_KEY, "depth_charts", str(exc)) from exc

        records = []
        for row in _rows(frame):
            if week is not None and _opt_int(row.get("week")) != week:
                continue
            gsis_id = row.get("gsis_id")
            if not gsis_id:
                continue
            records.append(
                {
                    "player_external_id": str(gsis_id),
                    "season": _opt_int(row.get("season")),
                    "week": _opt_int(row.get("week")),
                    "team": _norm_team(row.get("club_code") or row.get("team")),
                    "position": row.get("depth_position") or row.get("position"),
                    "rank": _opt_int(row.get("depth_team")) or 99,
                    "role": row.get("formation"),
                }
            )
        return self._result(records)

    # -- usage ---------------------------------------------------------------
    def fetch_usage(self, season: int | None = None, week: int | None = None, **_: Any):
        """Snap share, keyed by ``pfr_id`` rather than ``gsis_id``.

        The mismatch is why the roster crosswalk is the hub: callers translate
        pfr -> canonical through it.
        """
        season = season or self._season
        nfl = _nfl()
        try:
            frame = nfl.load_snap_counts(seasons=[season])
        except Exception as exc:  # noqa: BLE001
            raise ProviderUnavailable(PROVIDER_KEY, "usage", str(exc)) from exc

        records = []
        for row in _rows(frame):
            if week is not None and _opt_int(row.get("week")) != week:
                continue
            pfr_id = row.get("pfr_player_id")
            if not pfr_id:
                continue
            records.append(
                {
                    "player_pfr_id": str(pfr_id),
                    "player_name": row.get("player"),
                    "season": _opt_int(row.get("season")),
                    "week": _opt_int(row.get("week")),
                    "team": _norm_team(row.get("team")),
                    "position": row.get("position"),
                    "snap_pct": _pct(row.get("offense_pct")),
                    "offense_snaps": _opt_int(row.get("offense_snaps")),
                }
            )
        return self._result(records, notes=["keyed by pfr_id; join via roster crosswalk"])

    # -- rankings (optional supplement) --------------------------------------
    def fetch_rankings(self, **_: Any) -> FetchResult:
        """FantasyPros expert consensus, proxied via DynastyProcess.

        Flagged optional: the upstream repository is GPL-3.0, so this is a
        supplement to be enabled deliberately, never a load-bearing input.
        """
        nfl = _nfl()
        try:
            frame = nfl.load_ff_rankings(type="week")
        except Exception as exc:  # noqa: BLE001
            raise ProviderUnavailable(PROVIDER_KEY, "rankings", str(exc)) from exc

        records = []
        for row in _rows(frame):
            records.append(
                {
                    "player_name": row.get("player") or row.get("player_name"),
                    "position": row.get("pos"),
                    "team": _norm_team(row.get("team")),
                    "overall_rank": _opt_int(row.get("ecr")),
                    "position_rank": _opt_int(row.get("pos_ecr")),
                    "expert_consensus_stdev": _opt_float(row.get("sd")),
                    "best": _opt_int(row.get("best")),
                    "worst": _opt_int(row.get("worst")),
                }
            )
        return self._result(
            records,
            notes=["source repository is GPL-3.0; treat as optional supplement"],
        )


# --------------------------------------------------------------------------
def _opt_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _opt_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _pct(value: Any) -> float | None:
    """nflverse snap percentages arrive as 0-1 fractions."""
    parsed = _opt_float(value)
    if parsed is None:
        return None
    return round(parsed * 100.0, 2) if parsed <= 1.0 else round(parsed, 2)


def _kickoff(gameday: Any, gametime: Any) -> datetime | None:
    """Combine nflverse's separate date and ET time columns into one UTC stamp."""
    if not gameday:
        return None
    try:
        date_part = datetime.fromisoformat(str(gameday)[:10]).date()
    except ValueError:
        return None
    hour, minute = 13, 0
    if gametime:
        # A malformed time falls back to a 1pm ET default rather than dropping
        # the game; kickoff precision is not worth losing the row over.
        with contextlib.suppress(ValueError, TypeError):
            hour, minute = (int(p) for p in str(gametime).split(":")[:2])
    # nflverse gametime is US/Eastern. Add the UTC offset as a timedelta so a
    # Sunday-night 8:20pm ET kickoff correctly becomes Monday 00:20 UTC.
    naive = datetime.combine(date_part, time(hour, minute))
    offset_hours = 4 if 3 <= naive.month <= 10 else 5
    return naive.replace(tzinfo=UTC) + timedelta(hours=offset_hours)


@register("nflverse")
def _build(settings: Settings) -> NflverseProvider:
    return NflverseProvider(settings)
