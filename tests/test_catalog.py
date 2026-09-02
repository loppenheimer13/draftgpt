"""The league catalogue."""

from __future__ import annotations

import pytest

from huddle.catalog import LEAGUES, get_league, leagues_in_season


def test_every_league_is_addressable() -> None:
    for spec in LEAGUES:
        assert get_league(spec.key) is spec
        assert spec.provider_path.count("/") == 1


def test_spoken_names_are_readable_aloud() -> None:
    """A synthesiser reads "NFL" as a word; letters must be spaced."""
    for spec in LEAGUES:
        assert "NFL" not in spec.spoken_name
        assert "WNBA" not in spec.spoken_name
        assert spec.spoken_name == spec.spoken_name.strip()


def test_all_four_requested_families_of_sport_are_covered() -> None:
    keys = {spec.key for spec in LEAGUES}
    assert {"nfl", "nba", "mlb", "nhl", "wnba"} <= keys
    assert {"ncaaf", "ncaam", "ncaaw"} <= keys
    assert {"epl", "mls", "nwsl"} <= keys


def test_season_windows_are_plausible() -> None:
    assert "nfl" in {s.key for s in leagues_in_season(11)}
    assert "nfl" not in {s.key for s in leagues_in_season(6)}
    assert "wnba" in {s.key for s in leagues_in_season(7)}


def test_unknown_league_raises_with_a_useful_message() -> None:
    with pytest.raises(KeyError, match="known"):
        get_league("quidditch")
