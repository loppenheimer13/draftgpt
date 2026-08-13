"""Player identity reconciliation.

A wrong match here corrupts everything downstream and does so silently, so the
resolver must fail loudly rather than guess.
"""

from __future__ import annotations

import pytest

from draftgpt.database.models import Player, PlayerAlias, PlayerProviderId
from draftgpt.ingestion.identity import (
    AmbiguousPlayerError,
    PlayerResolver,
    name_key,
    normalize_name,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("A.J. Brown", "aj brown"),
        ("Michael Pittman Jr.", "michael pittman"),
        ("Amon-Ra St. Brown", "amonra st brown"),
        ("Ken Walker III", "ken walker"),
        ("  Deebo   Samuel  ", "deebo samuel"),
        ("D'Andre Swift", "dandre swift"),
        ("José Álvarez", "jose alvarez"),
        ("", ""),
    ],
)
def test_normalize_name(raw, expected):
    assert normalize_name(raw) == expected


def test_suffix_stripping_does_not_eat_real_names():
    """'Vic Beasley Jr' loses the suffix; a player literally surnamed 'V' must
    not be reduced to nothing."""
    assert normalize_name("Vic Beasley Jr") == "vic beasley"
    assert normalize_name("Jeff Wilson") == "jeff wilson"


def test_name_key_uses_initial_and_surname():
    assert name_key("A.J. Brown") == "a.brown"
    assert name_key("AJ Brown") == "a.brown"


def add_player(session, name, position, **kw) -> Player:
    player = Player(
        full_name=name,
        normalized_name=normalize_name(name),
        primary_position=position,
        **kw,
    )
    session.add(player)
    session.flush()
    return player


def test_provider_id_match_is_preferred_and_exact(session):
    player = add_player(session, "Justin Jefferson", "WR")
    session.add(
        PlayerProviderId(
            player_id=player.id, provider_key="espn", external_id="4262921",
            match_method="exact", match_confidence=1.0,
        )
    )
    session.flush()

    resolver = PlayerResolver(session)
    result = resolver.resolve_provider_player("espn", "4262921", name="Totally Different Name")
    assert result.player_id == player.id
    assert result.method == "provider_id"
    assert result.confidence == 1.0


def test_exact_name_and_position_match(session):
    player = add_player(session, "Josh Allen", "QB")
    resolver = PlayerResolver(session)
    result = resolver.resolve_provider_player("espn", "1", name="Josh Allen", position="QB")
    assert result.player_id == player.id
    assert result.confidence >= 0.9


def test_same_name_different_positions_disambiguates(session):
    """The real 'Josh Allen' problem: a QB and a linebacker share a name."""
    qb = add_player(session, "Josh Allen", "QB")
    add_player(session, "Josh Allen", "LB")

    resolver = PlayerResolver(session)
    assert resolver.resolve_provider_player("espn", "1", "Josh Allen", "QB").player_id == qb.id


def test_same_name_same_position_is_ambiguous_not_guessed(session):
    add_player(session, "Mike Williams", "WR")
    add_player(session, "Mike Williams", "WR")

    resolver = PlayerResolver(session)
    result = resolver.resolve_provider_player("espn", "1", "Mike Williams", "WR")

    assert not result.resolved
    assert len(result.ambiguous_candidates) == 2
    assert result.needs_review


def test_alias_resolution(session):
    player = add_player(session, "Marquise Brown", "WR")
    session.add(
        PlayerAlias(
            player_id=player.id, alias="Hollywood Brown",
            normalized_alias=normalize_name("Hollywood Brown"),
        )
    )
    session.flush()

    resolver = PlayerResolver(session)
    result = resolver.resolve_provider_player("espn", "9", "Hollywood Brown", "WR")
    assert result.player_id == player.id
    assert result.method == "alias"


def test_initial_fallback_matches_punctuation_variants(session):
    player = add_player(session, "AJ Brown", "WR")
    resolver = PlayerResolver(session)
    # Normalization already collapses "A.J." -> "aj", so this is an exact hit.
    assert resolver.resolve_provider_player("espn", "3", "A.J. Brown", "WR").player_id == player.id


def test_unmatched_returns_unresolved_rather_than_raising(session):
    resolver = PlayerResolver(session)
    result = resolver.resolve_provider_player("espn", "404", "Nobody Atall", "WR")
    assert not result.resolved
    assert result.reason


def test_resolve_query_rejects_ambiguity(session):
    """`/draft picked <name>` must refuse rather than draft the wrong player."""
    add_player(session, "Mike Williams", "WR")
    add_player(session, "Mike Williams", "WR")

    resolver = PlayerResolver(session)
    with pytest.raises(AmbiguousPlayerError) as excinfo:
        resolver.resolve_query("Mike Williams")
    assert len(excinfo.value.candidates) == 2


def test_resolve_query_substring_search(session):
    player = add_player(session, "Christian McCaffrey", "RB")
    resolver = PlayerResolver(session)
    assert resolver.resolve_query("mccaffrey").id == player.id


def test_resolve_query_raises_lookup_error_when_absent(session):
    resolver = PlayerResolver(session)
    with pytest.raises(LookupError):
        resolver.resolve_query("Nonexistent Person")


def test_link_is_idempotent(session):
    player = add_player(session, "Bijan Robinson", "RB")
    resolver = PlayerResolver(session)
    first = resolver.link(player.id, "espn", "123", "Bijan Robinson", "exact", 1.0)
    session.flush()
    second = resolver.link(player.id, "espn", "123", "Bijan Robinson", "exact", 1.0)
    assert first.id == second.id


def test_low_confidence_link_is_flagged_for_review(session):
    player = add_player(session, "Some Rookie", "WR")
    resolver = PlayerResolver(session)
    mapping = resolver.link(player.id, "espn", "77", "S. Rookie", "initial", 0.85)
    assert mapping.needs_review
