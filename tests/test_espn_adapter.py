"""ESPN payload normalization.

ESPN publishes no schema, so these tests pin our interpretation of it against
recorded payload shapes. The most important assertion in this file is that an
*unrecognised scoring rule is never silently dropped* -- that failure mode would
corrupt every projection with no visible symptom.
"""

from __future__ import annotations

import httpx
import pytest

from draftgpt.config import Settings
from draftgpt.providers.base import ProviderAuthError, ProviderUnavailable
from draftgpt.providers.espn.adapter import (
    EspnLeagueProvider,
    _normalize_roster_slots,
    _normalize_scoring,
    _playoff_weeks,
)
from draftgpt.providers.espn.client import EspnClient

# Stat ids: 53=receptions, 42=rec yards, 43=rec TD, 3=pass yards, 20=INT.
SCORING_ITEMS = [
    {"statId": 53, "points": 1.0},
    {"statId": 42, "points": 0.1},
    {"statId": 43, "points": 6.0},
    {"statId": 3, "points": 0.04},
    {"statId": 20, "points": -2.0},
]


def test_scoring_normalizes_to_canonical_keys():
    scoring, unmapped = _normalize_scoring(SCORING_ITEMS)
    assert scoring["rec"] == 1.0
    assert scoring["rec_yd"] == 0.1
    assert scoring["pass_int"] == -2.0
    assert unmapped == []


def test_unmapped_scoring_rule_is_reported_not_dropped():
    """The critical safety property of this adapter."""
    items = [*SCORING_ITEMS, {"statId": 99999, "points": 5.0}]
    scoring, unmapped = _normalize_scoring(items)

    assert unmapped == [{"stat_id": 99999, "points": 5.0}]
    assert 5.0 not in scoring.values(), "unknown rule must not be guessed into a key"


def test_zero_valued_unknown_rule_is_not_flagged():
    """A rule worth zero points cannot change any score, so flagging it would
    be noise that trains the owner to ignore real warnings."""
    _, unmapped = _normalize_scoring([{"statId": 99999, "points": 0.0}])
    assert unmapped == []


def test_duplicate_canonical_keys_are_summed_not_overwritten():
    """ESPN ids 41 and 58 both mean 'targets'. Letting one silently win would
    lose half the configured value."""
    scoring, _ = _normalize_scoring([{"statId": 41, "points": 0.5}, {"statId": 58, "points": 0.5}])
    assert scoring["rec_tgt"] == pytest.approx(1.0)


def test_points_allowed_brackets_become_a_tier_table():
    items = [
        {"statId": 89, "points": 10.0},   # 0 points allowed
        {"statId": 90, "points": 7.0},    # 1-6
        {"statId": 91, "points": 4.0},    # 7-13
    ]
    scoring, unmapped = _normalize_scoring(items)
    tiers = scoring["points_allowed_tiers"]

    assert unmapped == []
    assert len(tiers) == 3
    assert tiers[0] == {"min": 0.0, "max": 0.0, "points": 10.0}
    assert tiers == sorted(tiers, key=lambda t: t["min"])


def test_roster_slots_expand_with_eligibility():
    # 0=QB, 2=RB, 4=WR, 6=TE, 23=FLEX, 20=BE
    slots = _normalize_roster_slots({"0": 1, "2": 2, "4": 2, "6": 1, "23": 1, "20": 6})
    by_slot = {s["slot"]: s for s in slots}

    assert by_slot["RB"]["count"] == 2
    assert set(by_slot["FLEX"]["eligible_positions"]) == {"RB", "WR", "TE"}
    assert by_slot["FLEX"]["is_starting"]
    assert not by_slot["BE"]["is_starting"]


def test_zero_count_slots_are_omitted():
    slots = _normalize_roster_slots({"0": 1, "17": 0})
    assert [s["slot"] for s in slots] == ["QB"]


def test_unknown_slot_id_falls_back_to_bench_not_a_crash():
    slots = _normalize_roster_slots({"0": 1, "999": 1})
    assert len(slots) == 2
    assert any(s["slot"] == "BE" for s in slots)


def test_playoff_weeks_derived_from_schedule_settings():
    weeks = _playoff_weeks(
        {"matchupPeriodCount": 14, "playoffTeamCount": 6, "playoffMatchupPeriodLength": 1}
    )
    assert weeks == [15, 16, 17]


def test_playoff_weeks_empty_when_no_playoffs():
    assert _playoff_weeks({"matchupPeriodCount": 14, "playoffTeamCount": 0}) == []


# --------------------------------------------------------------------------
# Client error handling
# --------------------------------------------------------------------------
def client_with(status_code: int, json_body: dict | None = None) -> EspnClient:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=json_body or {})

    return EspnClient(
        league_id="123", season=2026, transport=httpx.MockTransport(handler)
    )


def test_private_league_without_cookies_raises_auth_error():
    with client_with(401) as client, pytest.raises(ProviderAuthError) as excinfo:
        client.get("mSettings")
    assert "DRAFTGPT_ESPN_S2" in str(excinfo.value)


def test_missing_league_raises_unavailable():
    with client_with(404) as client, pytest.raises(ProviderUnavailable):
        client.get("mSettings")


def test_rate_limit_is_retryable_unavailable():
    with client_with(429) as client, pytest.raises(ProviderUnavailable) as excinfo:
        client.get("mSettings")
    assert "rate limited" in str(excinfo.value)


def test_historical_single_element_list_is_unwrapped():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"id": 123, "seasonId": 2026}])

    with EspnClient("123", 2026, transport=httpx.MockTransport(handler)) as client:
        assert client.get("mSettings")["id"] == 123


def test_settings_fetch_normalizes_end_to_end():
    payload = {
        "id": 987654,
        "seasonId": 2026,
        "scoringPeriodId": 3,
        "status": {"isActive": True, "currentMatchupPeriod": 3, "latestScoringPeriod": 3},
        "draftDetail": {"drafted": True, "inProgress": False},
        "teams": [{"id": i} for i in range(1, 11)],
        "settings": {
            "name": "Test League",
            "scoringSettings": {"scoringItems": SCORING_ITEMS},
            "rosterSettings": {"lineupSlotCounts": {"0": 1, "2": 2, "4": 2, "23": 1, "20": 5}},
            "scheduleSettings": {"matchupPeriodCount": 14, "playoffTeamCount": 4},
            "acquisitionSettings": {"isUsingAcquisitionBudget": True, "acquisitionBudget": 100},
            "tradeSettings": {},
        },
    }

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    client = EspnClient("987654", 2026, transport=httpx.MockTransport(handler))
    provider = EspnLeagueProvider(Settings(espn_league_id="987654", season=2026), client=client)

    result = provider.fetch("league_settings")
    record = result.records[0]

    assert record["name"] == "Test League"
    assert record["team_count"] == 10
    assert record["waiver_type"] == "faab"
    assert record["faab_budget"] == 100
    assert record["scoring"]["rec"] == 1.0
    assert record["status"] == "in_season"
    assert result.notes == [], "clean payload should raise no warnings"


def test_unmapped_scoring_surfaces_as_a_fetch_note():
    payload = {
        "id": 1, "seasonId": 2026, "teams": [], "status": {},
        "settings": {
            "scoringSettings": {"scoringItems": [{"statId": 88888, "points": 3.0}]},
            "rosterSettings": {"lineupSlotCounts": {}},
            "scheduleSettings": {}, "acquisitionSettings": {}, "tradeSettings": {},
        },
    }

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    provider = EspnLeagueProvider(
        Settings(espn_league_id="1", season=2026),
        client=EspnClient("1", 2026, transport=httpx.MockTransport(handler)),
    )
    result = provider.fetch("league_settings")
    assert any("no canonical mapping" in note for note in result.notes)


def test_cookie_jar_normalizes_swid_braces():
    settings = Settings(espn_s2="abc", espn_swid="1234-5678")
    assert settings.espn_cookies["SWID"] == "{1234-5678}"
    assert Settings(espn_s2="abc", espn_swid="{1234}").espn_cookies["SWID"] == "{1234}"


def test_no_cookies_when_credentials_absent():
    assert Settings(espn_league_id="1").espn_cookies == {}


# --------------------------------------------------------------------------
# Projections
# --------------------------------------------------------------------------
def player_pool_payload(stats: list[dict]) -> dict:
    return {
        "players": [
            {
                "player": {
                    "id": 3139477,
                    "fullName": "Test Quarterback",
                    "defaultPositionId": 1,
                    "proTeamId": 12,
                    "stats": stats,
                }
            }
        ]
    }


def projection_provider(payload: dict) -> EspnLeagueProvider:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    return EspnLeagueProvider(
        Settings(espn_league_id="1", season=2026),
        client=EspnClient("1", 2026, transport=httpx.MockTransport(handler)),
    )


WEEK_PROJECTION = {
    "scoringPeriodId": 3,
    "seasonId": 2026,
    "statSourceId": 1,   # projected
    "statSplitTypeId": 1,  # single week
    "appliedTotal": 22.4,
    "stats": {"3": 275.0, "4": 2.0, "20": 0.5, "24": 15.0},
}


def test_weekly_projection_extracts_canonical_stat_line():
    provider = projection_provider(player_pool_payload([WEEK_PROJECTION]))
    record = provider.fetch("projections_weekly", week=3).records[0]

    assert record["player_external_id"] == "3139477"
    assert record["week"] == 3
    assert record["scope"] == "week"
    assert record["stat_line"]["pass_yd"] == 275.0
    assert record["stat_line"]["pass_td"] == 2.0
    assert record["stat_line"]["rush_yd"] == 15.0


def test_projection_does_not_trust_espn_applied_total():
    """The league's own scoring engine must price the stat line; ESPN's total is
    kept only as a cross-check."""
    provider = projection_provider(player_pool_payload([WEEK_PROJECTION]))
    record = provider.fetch("projections_weekly", week=3).records[0]

    assert record.get("projected_points") is None
    assert record["provider_applied_total"] == 22.4


def test_actual_results_are_not_mistaken_for_projections():
    """statSourceId 0 is what a player actually scored. Ingesting it as a
    projection would leak future information into a decision."""
    actual = {**WEEK_PROJECTION, "statSourceId": 0}
    provider = projection_provider(player_pool_payload([actual]))
    assert provider.fetch("projections_weekly", week=3).records == []


def test_wrong_week_is_not_returned():
    provider = projection_provider(player_pool_payload([WEEK_PROJECTION]))
    assert provider.fetch("projections_weekly", week=9).records == []


def test_season_scope_selects_the_season_split():
    season_entry = {
        "seasonId": 2026, "statSourceId": 1, "statSplitTypeId": 0,
        "appliedTotal": 310.0, "stats": {"3": 4200.0, "4": 30.0},
    }
    provider = projection_provider(player_pool_payload([season_entry, WEEK_PROJECTION]))
    records = provider.fetch("projections_season").records

    assert len(records) == 1
    assert records[0]["scope"] == "season"
    assert records[0]["week"] is None
    assert records[0]["stat_line"]["pass_yd"] == 4200.0


def test_stat_id_mismatch_surfaces_as_a_warning():
    """If our map is wrong the re-scored total drifts from ESPN's. That is the
    earliest signal available and must not be silent."""
    broken = {
        **WEEK_PROJECTION,
        "appliedTotal": 99.0,  # wildly inconsistent with the stat line
    }
    provider = projection_provider(player_pool_payload([broken]))
    result = provider.fetch("projections_weekly", week=3)
    assert any("stat-id mapping gap" in note for note in result.notes)


def test_consistent_totals_produce_no_warning():
    provider = projection_provider(player_pool_payload([WEEK_PROJECTION]))
    result = provider.fetch("projections_weekly", week=3)
    assert not any("mapping gap" in note for note in result.notes)


def test_player_with_no_projection_entry_is_skipped_not_zeroed():
    provider = projection_provider(player_pool_payload([]))
    assert provider.fetch("projections_weekly", week=3).records == []
