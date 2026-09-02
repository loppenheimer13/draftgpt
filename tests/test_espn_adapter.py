"""The ESPN adapter, against recorded payload shapes."""

from __future__ import annotations

import httpx
import pytest

from huddle.config import Settings
from huddle.providers.base import ProviderUnavailable
from huddle.providers.espn import EspnProvider

SCOREBOARD = {
    "events": [{
        "id": "401",
        "date": "2026-09-01T23:00Z",
        "name": "Atlanta Braves at Washington Nationals",
        "competitions": [{
            "venue": {"fullName": "Nationals Park"},
            "notes": [{"type": "event", "headline": "Rivalry Week"}],
            "broadcasts": [{"names": ["MLB.TV"]}],
            "status": {"type": {"state": "post", "completed": True, "detail": "Final"}},
            "competitors": [
                {"homeAway": "home", "score": {"value": 9.0, "displayValue": "9"},
                 "team": {"id": "20", "displayName": "Washington Nationals"}},
                {"homeAway": "away", "score": "5",
                 "team": {"id": "15", "displayName": "Atlanta Braves"}},
            ],
        }],
    }]
}


def provider(payload, status: int = 200) -> EspnProvider:
    return EspnProvider(
        Settings(),
        httpx.Client(transport=httpx.MockTransport(
            lambda r: httpx.Response(status, json=payload)
        )),
    )


def test_scoreboard_normalizes_both_score_shapes() -> None:
    """ESPN returns a score as an object in some sports and a bare string in
    others; a normalizer that handles one silently loses the other."""
    record = provider(SCOREBOARD).fetch("scoreboard", league="mlb").records[0]
    assert record["home_score"] == 9
    assert record["away_score"] == 5
    assert record["state"] == "post"
    assert record["broadcast"] == "MLB.TV"
    assert record["extra"]["notes"] == ["Rivalry Week"]


def test_pregame_scores_are_none_not_zero() -> None:
    payload = {"events": [{
        "id": "1", "date": "2026-09-05T23:00Z", "name": "A at B",
        "competitions": [{
            "status": {"type": {"state": "pre", "completed": False, "detail": "Fri 7:00"}},
            "competitors": [
                {"homeAway": "home", "score": "0", "team": {"id": "1", "displayName": "B"}},
                {"homeAway": "away", "score": "0", "team": {"id": "2", "displayName": "A"}},
            ],
        }],
    }]}
    # A literal "0" before kickoff is a real zero from the feed; what must not
    # happen is an absent score becoming one.
    record = provider(payload).fetch("scoreboard", league="mlb").records[0]
    assert record["state"] == "pre"

    payload["events"][0]["competitions"][0]["competitors"][0]["score"] = None
    record = provider(payload).fetch("scoreboard", league="mlb").records[0]
    assert record["home_score"] is None


def test_teams_build_a_searchable_name() -> None:
    payload = {"sports": [{"leagues": [{"teams": [{"team": {
        "id": "29", "displayName": "Arizona Diamondbacks", "name": "Diamondbacks",
        "location": "Arizona", "abbreviation": "ARI", "slug": "arizona-diamondbacks",
        "isActive": True, "logos": [{"href": "http://x/logo.png"}],
    }}]}]}]}
    record = provider(payload).fetch("league_teams", league="mlb").records[0]
    assert record["short_name"] == "Diamondbacks"
    assert "arizona" in record["search_name"] and "ari" in record["search_name"]
    assert record["logo_url"] == "http://x/logo.png"


def test_roster_extracts_birthdays_for_birthday_club() -> None:
    payload = {"athletes": [{"items": [{
        "id": "5", "fullName": "Chris Cenac Jr.", "dateOfBirth": "2007-02-01T08:00Z",
        "position": {"displayName": "Forward"}, "jersey": "12",
        "birthPlace": {"city": "New Orleans", "state": "LA"},
    }]}]}
    record = provider(payload).fetch("team_roster", league="nba", team_external_id="2").records[0]
    assert record["birth_month"] == 2 and record["birth_day"] == 1
    assert record["birth_place"] == "New Orleans, LA"


def test_news_is_returned_unjudged() -> None:
    """The adapter must not filter. Safety is a separate, testable gate."""
    payload = {"articles": [{
        "id": 1, "headline": "Player arrested", "description": "A long summary here.",
        "type": "Story", "categories": [{"description": "MLB"}],
        "links": {"web": {"href": "http://x"}},
    }]}
    records = provider(payload).fetch("league_news", league="mlb").records
    assert records[0]["headline"] == "Player arrested"
    assert records[0]["type"] == "story"


def test_http_failure_becomes_a_typed_provider_error() -> None:
    with pytest.raises(ProviderUnavailable):
        provider({}, status=503).fetch("scoreboard", league="mlb")


def test_unknown_league_is_refused() -> None:
    with pytest.raises(KeyError):
        provider(SCOREBOARD).fetch("scoreboard", league="quidditch")


def test_adapter_declares_no_write_capability() -> None:
    """Read-only by construction, not by policy."""
    import inspect

    from huddle.providers import espn

    source = inspect.getsource(espn)
    for verb in (".post(", ".put(", ".patch(", ".delete("):
        assert verb not in source, f"adapter must never {verb}"
