"""ESPN league provider adapter.

Normalizes ESPN's payloads into the canonical shapes the ingestion layer
persists. Nothing here touches the database.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from draftgpt.config import Settings
from draftgpt.domain.enums import Capability, FreshnessClass, SlotType
from draftgpt.providers.base import (
    FetchResult,
    LeagueProvider,
    ProviderError,
    ProviderSpec,
)
from draftgpt.providers.espn import constants as C
from draftgpt.providers.espn.client import EspnClient
from draftgpt.providers.registry import register

SPEC = ProviderSpec(
    key="espn",
    display_name="ESPN Fantasy Football",
    categories=("league", "registry", "market"),
    capabilities=(
        Capability.LEAGUE_SETTINGS,
        Capability.LEAGUE_ROSTERS,
        Capability.LEAGUE_MATCHUPS,
        Capability.LEAGUE_TRANSACTIONS,
        Capability.LEAGUE_DRAFT,
        Capability.LEAGUE_FREE_AGENTS,
        Capability.PLAYER_REGISTRY,
        Capability.MARKET_TRENDS,
        Capability.INJURIES,
    ),
    freshness_class=FreshnessClass.RAPID,
    requires_auth=False,
    rate_limit_per_min=60,
    raw_retention_days=14,
    license_note=(
        "Undocumented but publicly readable JSON endpoints used by ESPN's own web "
        "client. Read-only GETs at low volume for a league the user is a member of. "
        "No scraping of rendered HTML, no redistribution of payloads."
    ),
    fallback="fall back to the last successful sync; warn and reduce confidence",
)


class EspnLeagueProvider(LeagueProvider):
    spec = SPEC

    def __init__(self, settings: Settings, client: EspnClient | None = None) -> None:
        self._settings = settings
        self._client = client
        self._league_id = settings.espn_league_id
        self._season = settings.season

    # -- plumbing ---------------------------------------------------------
    @property
    def client(self) -> EspnClient:
        if self._client is None:
            if not self._league_id:
                raise ProviderError(
                    "espn", "config", "DRAFTGPT_ESPN_LEAGUE_ID is not set"
                )
            self._client = EspnClient(
                league_id=self._league_id,
                season=self._season,
                cookies=self._settings.espn_cookies,
                timeout=self._settings.http_timeout_seconds,
            )
        return self._client

    def health_check(self) -> tuple[bool, str]:
        if not self._league_id:
            return False, "no league id configured"
        try:
            self.client.get("mSettings")
        except Exception as exc:  # noqa: BLE001 - surfaced as health text, never raised
            return False, str(exc)
        return True, "ok"

    def _result(self, records: list[dict[str, Any]], **kw: Any) -> FetchResult:
        return FetchResult(
            capability="",
            records=records,
            retrieved_at=datetime.now(UTC),
            **kw,
        )

    # -- capabilities -----------------------------------------------------
    def fetch_league_settings(self, **_: Any) -> FetchResult:
        payload = self.client.get(["mSettings", "mTeam"])
        settings = payload.get("settings", {}) or {}
        scoring_settings = settings.get("scoringSettings", {}) or {}
        roster_settings = settings.get("rosterSettings", {}) or {}
        schedule_settings = settings.get("scheduleSettings", {}) or {}
        acquisition = settings.get("acquisitionSettings", {}) or {}
        trade_settings = settings.get("tradeSettings", {}) or {}

        scoring, unmapped = _normalize_scoring(scoring_settings.get("scoringItems", []) or [])
        slots = _normalize_roster_slots(roster_settings.get("lineupSlotCounts", {}) or {})

        record = {
            "external_id": str(payload.get("id", self._league_id)),
            "name": settings.get("name") or f"ESPN League {self._league_id}",
            "season": int(payload.get("seasonId", self._season)),
            "team_count": len(payload.get("teams", []) or [])
            or int(settings.get("size", 0) or 0),
            "current_week": payload.get("scoringPeriodId"),
            "regular_season_weeks": schedule_settings.get("matchupPeriodCount", 14),
            "playoff_team_count": schedule_settings.get("playoffTeamCount"),
            "playoff_weeks": _playoff_weeks(schedule_settings),
            "status": C.league_status(
                is_active=bool(payload.get("status", {}).get("isActive", True)),
                current_matchup_period=int(
                    payload.get("status", {}).get("currentMatchupPeriod", 0) or 0
                ),
                draft_complete=bool(
                    payload.get("status", {}).get("latestScoringPeriod", 0)
                    and not payload.get("draftDetail", {}).get("inProgress", False)
                    and payload.get("draftDetail", {}).get("drafted", False)
                ),
            ),
            "format": "keeper" if settings.get("isKeeperLeague") else "redraft",
            "scoring": scoring,
            "roster_slots": slots,
            "roster_size": sum(s["count"] for s in slots),
            "ir_slots": next(
                (s["count"] for s in slots if s["slot"] == SlotType.IR), 0
            ),
            "waiver_type": "faab" if acquisition.get("isUsingAcquisitionBudget") else "rolling",
            "faab_budget": acquisition.get("acquisitionBudget"),
            "waiver_process_days": acquisition.get("waiverProcessDays", []) or [],
            "trade_deadline_at": _epoch_ms(trade_settings.get("deadlineDate")),
            "trade_review_type": trade_settings.get("revisionHours") and "review_period" or None,
            "raw_settings": {
                "unmapped_scoring_items": unmapped,
                "acquisition": acquisition,
                "schedule": schedule_settings,
                "roster": roster_settings,
            },
        }
        notes = []
        if unmapped:
            notes.append(
                f"{len(unmapped)} ESPN scoring item(s) had no canonical mapping and were "
                "preserved unscored -- run `draftgpt sources verify-espn` before trusting "
                "projections"
            )
        return self._result([record], notes=notes)

    def fetch_league_rosters(self, **_: Any) -> FetchResult:
        payload = self.client.get(["mRoster", "mTeam"])
        records: list[dict[str, Any]] = []
        for team in payload.get("teams", []) or []:
            entries = (team.get("roster") or {}).get("entries", []) or []
            records.append(
                {
                    "team_external_id": str(team.get("id")),
                    "team_name": _team_name(team),
                    "owner_external_ids": team.get("owners", []) or [],
                    "wins": (team.get("record", {}).get("overall", {}) or {}).get("wins", 0),
                    "losses": (team.get("record", {}).get("overall", {}) or {}).get("losses", 0),
                    "ties": (team.get("record", {}).get("overall", {}) or {}).get("ties", 0),
                    "points_for": (team.get("record", {}).get("overall", {}) or {}).get(
                        "pointsFor", 0.0
                    ),
                    "points_against": (team.get("record", {}).get("overall", {}) or {}).get(
                        "pointsAgainst", 0.0
                    ),
                    "faab_remaining": _faab_remaining(team),
                    "waiver_priority": team.get("waiverRank"),
                    "draft_position": team.get("draftDayProjectedRank"),
                    "players": [_normalize_roster_entry(e) for e in entries],
                }
            )
        return self._result(records)

    def fetch_league_free_agents(self, limit: int = 400, week: int | None = None) -> FetchResult:
        payload = self.client.player_pool(limit=limit, scoring_period_id=week)
        records = [
            _normalize_player_pool_entry(p)
            for p in (payload.get("players", []) or [])
        ]
        return self._result([r for r in records if r])

    def fetch_player_registry(self, limit: int = 1200) -> FetchResult:
        payload = self.client.player_pool(limit=limit, status="ALL")
        records = [
            _normalize_player_pool_entry(p) for p in (payload.get("players", []) or [])
        ]
        return self._result([r for r in records if r])

    def fetch_league_draft(self, **_: Any) -> FetchResult:
        payload = self.client.get(["mDraftDetail", "mSettings", "mTeam"])
        detail = payload.get("draftDetail", {}) or {}
        settings = (payload.get("settings", {}) or {}).get("draftSettings", {}) or {}
        roster_settings = (payload.get("settings", {}) or {}).get("rosterSettings", {}) or {}
        team_count = len(payload.get("teams", []) or [])
        rounds = sum((roster_settings.get("lineupSlotCounts", {}) or {}).values()) or 16

        picks = [
            {
                "overall_pick": p.get("overallPickNumber"),
                "round": p.get("roundId"),
                "pick_in_round": p.get("roundPickNumber"),
                "team_external_id": str(p.get("teamId")),
                "player_external_id": str(p.get("playerId")),
                "auction_price": p.get("bidAmount") or None,
                "is_keeper": bool(p.get("keeper")),
            }
            for p in (detail.get("picks", []) or [])
        ]
        record = {
            "external_id": str(payload.get("id", self._league_id)),
            "type": "auction" if settings.get("type") == "AUCTION" else "snake",
            "rounds": int(rounds),
            "team_count": team_count,
            "seconds_per_pick": settings.get("timePerSelection"),
            "starts_at": _epoch_ms(settings.get("date")),
            "in_progress": bool(detail.get("inProgress")),
            "complete": bool(detail.get("drafted")),
            "pick_order": [str(t) for t in (settings.get("pickOrder", []) or [])],
            "picks": picks,
        }
        return self._result([record])

    def fetch_league_matchups(self, **_: Any) -> FetchResult:
        payload = self.client.get(["mMatchup", "mMatchupScore"])
        records = [
            {
                "week": m.get("matchupPeriodId"),
                "home_team_external_id": str((m.get("home") or {}).get("teamId")),
                "away_team_external_id": (
                    str((m.get("away") or {}).get("teamId")) if m.get("away") else None
                ),
                "home_score": (m.get("home") or {}).get("totalPoints"),
                "away_score": (m.get("away") or {}).get("totalPoints") if m.get("away") else None,
                "is_playoff": m.get("playoffTierType", "NONE") != "NONE",
                "status": (m.get("winner") or "UNDECIDED").lower(),
            }
            for m in (payload.get("schedule", []) or [])
        ]
        return self._result(records)

    def fetch_league_transactions(self, **_: Any) -> FetchResult:
        payload = self.client.get(["mTransactions2"], params={"scoringPeriodId": 0})
        records = []
        for tx in payload.get("transactions", []) or []:
            records.append(
                {
                    "external_id": str(tx.get("id")),
                    "type": C.TRANSACTION_TYPE.get(tx.get("type", ""), "roster_move"),
                    "status": (tx.get("status") or "EXECUTED").lower(),
                    "team_external_id": str(tx.get("teamId")) if tx.get("teamId") else None,
                    "bid_amount": tx.get("bidAmount"),
                    "processed_at": _epoch_ms(tx.get("processDate")),
                    "items": [
                        {
                            "player_external_id": str(i.get("playerId")),
                            "action": (i.get("type") or "").lower(),
                            "from_team_external_id": (
                                str(i.get("fromTeamId")) if i.get("fromTeamId") else None
                            ),
                            "to_team_external_id": (
                                str(i.get("toTeamId")) if i.get("toTeamId") else None
                            ),
                        }
                        for i in (tx.get("items", []) or [])
                    ],
                }
            )
        return self._result(records)

    def fetch_market_trends(self, limit: int = 400) -> FetchResult:
        payload = self.client.player_pool(limit=limit, status="ALL")
        records = []
        for entry in payload.get("players", []) or []:
            player = entry.get("player") or {}
            owned = player.get("ownership") or {}
            records.append(
                {
                    "player_external_id": str(player.get("id")),
                    "rostered_pct": owned.get("percentOwned"),
                    "started_pct": owned.get("percentStarted"),
                    "rostered_pct_change": owned.get("percentChange"),
                    "adds_24h": owned.get("auctionValueAverageChange"),
                }
            )
        return self._result(records)

    def fetch_injuries(self, limit: int = 800) -> FetchResult:
        payload = self.client.player_pool(limit=limit, status="ALL")
        records = []
        for entry in payload.get("players", []) or []:
            player = entry.get("player") or {}
            status = player.get("injuryStatus")
            if not status or status in {"ACTIVE", "NORMAL"}:
                continue
            records.append(
                {
                    "player_external_id": str(player.get("id")),
                    "event_type": "injury",
                    "designation": C.INJURY_STATUS.get(status, status),
                    "detail": None,
                }
            )
        return self._result(records)


# --------------------------------------------------------------------------
# Normalization helpers
# --------------------------------------------------------------------------
def _normalize_scoring(items: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """ESPN scoring items -> canonical scoring config plus unmapped leftovers."""
    scoring: dict[str, Any] = {}
    points_allowed_tiers: list[dict[str, float]] = []
    yards_allowed_tiers: list[dict[str, float]] = []
    unmapped: list[dict[str, Any]] = []

    for item in items:
        stat_id = item.get("statId")
        points = item.get("points")
        if stat_id is None:
            continue
        if points is None:
            points = (item.get("pointsOverrides") or {}).get("16", 0.0)
        points = float(points or 0.0)

        if stat_id in C.IGNORED_STAT_IDS:
            continue
        if stat_id in C.DEF_POINTS_ALLOWED_BRACKETS:
            lo, hi = C.DEF_POINTS_ALLOWED_BRACKETS[stat_id]
            points_allowed_tiers.append({"min": lo, "max": hi, "points": points})
            continue
        if stat_id in C.DEF_YARDS_ALLOWED_BRACKETS:
            lo, hi = C.DEF_YARDS_ALLOWED_BRACKETS[stat_id]
            yards_allowed_tiers.append({"min": lo, "max": hi, "points": points})
            continue

        key = C.STAT_BY_ID.get(stat_id)
        if key is None:
            if points:  # a zero-valued unknown rule cannot change any score
                unmapped.append({"stat_id": stat_id, "points": points})
            continue
        # Two ESPN ids can map to one canonical key (e.g. 41/58 -> rec_tgt);
        # summing preserves total value rather than letting one silently win.
        scoring[key] = scoring.get(key, 0.0) + points

    if points_allowed_tiers:
        scoring["points_allowed_tiers"] = sorted(points_allowed_tiers, key=lambda t: t["min"])
    if yards_allowed_tiers:
        scoring["yards_allowed_tiers"] = sorted(yards_allowed_tiers, key=lambda t: t["min"])
    return scoring, unmapped


def _normalize_roster_slots(lineup_slot_counts: dict[str, Any]) -> list[dict[str, Any]]:
    from draftgpt.domain.enums import SLOT_ELIGIBILITY

    slots: list[dict[str, Any]] = []
    for raw_id, count in lineup_slot_counts.items():
        count = int(count or 0)
        if count <= 0:
            continue
        slot_id = int(raw_id)
        slot = C.SLOT_BY_ID.get(slot_id)
        if slot is None:
            slot = SlotType.DP if slot_id in C.IDP_SLOT_IDS else SlotType.BENCH
        slots.append(
            {
                "slot": str(slot),
                "count": count,
                "eligible_positions": list(SLOT_ELIGIBILITY.get(str(slot), ())),
                "is_starting": str(slot) not in {SlotType.BENCH, SlotType.IR},
                "espn_slot_id": slot_id,
            }
        )
    return sorted(slots, key=lambda s: s["espn_slot_id"])


def _normalize_roster_entry(entry: dict[str, Any]) -> dict[str, Any]:
    pool = entry.get("playerPoolEntry") or {}
    player = pool.get("player") or {}
    return {
        "player_external_id": str(entry.get("playerId") or player.get("id")),
        "name": player.get("fullName"),
        "slot": str(C.SLOT_BY_ID.get(entry.get("lineupSlotId", 20), SlotType.BENCH)),
        "espn_slot_id": entry.get("lineupSlotId"),
        "acquisition_type": (entry.get("acquisitionType") or "").lower() or None,
        "position": C.POSITION_BY_ID.get(player.get("defaultPositionId", -1)),
        "pro_team": C.PRO_TEAM_BY_ID.get(player.get("proTeamId", 0), "FA"),
        "injury_status": C.INJURY_STATUS.get(
            player.get("injuryStatus", "ACTIVE"), player.get("injuryStatus")
        ),
    }


def _normalize_player_pool_entry(entry: dict[str, Any]) -> dict[str, Any] | None:
    player = entry.get("player") or entry.get("playerPoolEntry", {}).get("player") or {}
    if not player.get("id"):
        return None
    eligible = [
        C.SLOT_BY_ID.get(s)
        for s in (player.get("eligibleSlots", []) or [])
        if C.SLOT_BY_ID.get(s) not in {None, SlotType.BENCH, SlotType.IR}
    ]
    positions = sorted(
        {
            p
            for p in [C.POSITION_BY_ID.get(player.get("defaultPositionId", -1))]
            if p is not None
        }
    )
    return {
        "player_external_id": str(player.get("id")),
        "name": player.get("fullName"),
        "first_name": player.get("firstName"),
        "last_name": player.get("lastName"),
        "position": C.POSITION_BY_ID.get(player.get("defaultPositionId", -1)),
        "positions": positions,
        "eligible_slots": [str(s) for s in eligible if s],
        "pro_team": C.PRO_TEAM_BY_ID.get(player.get("proTeamId", 0), "FA"),
        "injury_status": C.INJURY_STATUS.get(
            player.get("injuryStatus", "ACTIVE"), player.get("injuryStatus")
        ),
        "is_team_defense": player.get("defaultPositionId") == 16,
        "rostered_pct": (player.get("ownership") or {}).get("percentOwned"),
        "on_team_external_id": (
            str(entry.get("onTeamId")) if entry.get("onTeamId") else None
        ),
    }


def _team_name(team: dict[str, Any]) -> str:
    name = team.get("name")
    if name:
        return str(name)
    location = (team.get("location") or "").strip()
    nickname = (team.get("nickname") or "").strip()
    return f"{location} {nickname}".strip() or f"Team {team.get('id')}"


def _faab_remaining(team: dict[str, Any]) -> int | None:
    spent = team.get("transactionCounter", {}).get("acquisitionBudgetSpent")
    return None if spent is None else int(spent)


def _playoff_weeks(schedule_settings: dict[str, Any]) -> list[int]:
    matchup_count = int(schedule_settings.get("matchupPeriodCount", 14) or 14)
    playoff_matchups = int(schedule_settings.get("playoffMatchupPeriodLength", 1) or 1)
    playoff_team_count = int(schedule_settings.get("playoffTeamCount", 0) or 0)
    if not playoff_team_count:
        return []
    rounds = max(1, (playoff_team_count - 1).bit_length())
    total = rounds * playoff_matchups
    return list(range(matchup_count + 1, matchup_count + 1 + total))


def _epoch_ms(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=UTC)
    except (TypeError, ValueError, OSError):
        return None


@register("espn")
def _build(settings: Settings) -> EspnLeagueProvider:
    return EspnLeagueProvider(settings)
