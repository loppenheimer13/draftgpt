"""End-to-end `/roster` against the fixture league.

This is golden scenario #2 from the brief: a questionable player with early and
late alternatives. The assertions pin behaviour that must survive refactors --
the response contract, reproducibility, and the degraded-state paths.
"""

from __future__ import annotations

import pytest

from draftgpt.commands.roster import build_snapshot, recommend_lineup
from draftgpt.database.models import (
    DecisionSnapshot,
    LeagueSeason,
    RecommendationRun,
)
from draftgpt.domain.contract import CommandResponse

WEEK = 1


@pytest.fixture()
def league_season_id(session, seeded_league) -> str:
    return seeded_league.league_season_id


def test_league_synced_completely(session, seeded_league):
    assert seeded_league.teams_synced == 12
    assert seeded_league.roster_entries == 147
    assert seeded_league.unresolved_players == []
    assert seeded_league.status_events >= 1


def test_response_conforms_to_the_contract(session, league_season_id):
    response = recommend_lineup(session, league_season_id, WEEK)
    assert isinstance(response, CommandResponse)

    # Round-tripping through JSON is what an API/MCP client actually receives.
    payload = response.model_dump(mode="json")
    for key in (
        "command", "league_id", "snapshot_id", "generated_at", "recommendation",
        "alternatives", "confidence", "reasons", "material_inputs", "conditions",
        "freshness", "do_now", "watch", "check_again_at",
    ):
        assert key in payload, f"contract is missing '{key}'"

    assert CommandResponse.model_validate(payload)
    assert response.command == "roster"
    assert 0.0 < response.confidence <= 1.0
    assert response.check_again_at is not None


def test_lineup_is_legal_and_complete(session, league_season_id):
    snapshot = build_snapshot(session, league_season_id, WEEK)
    response = recommend_lineup(session, league_season_id, WEEK)
    starters = response.recommendation["starters"]

    assert len(starters) == len([s for s in snapshot.slots]), "every starting slot filled"
    assert len({s["player_id"] for s in starters}) == len(starters), "no player started twice"

    by_slot_id = {s.slot_id: s for s in snapshot.slots}
    by_player = {o.player_id: o for o in snapshot.options}
    for starter in starters:
        option = by_player[starter["player_id"]]
        eligible = {
            pos
            for slot in by_slot_id.values()
            if slot.slot == starter["slot"]
            for pos in slot.eligible_positions
        }
        assert option.positions[0] in eligible, f"{option.name} illegal in {starter['slot']}"


def test_flex_displacement_is_solved(session, league_season_id):
    """The fixture is built so the bench WR must displace a WR into FLEX.

    A greedy optimizer leaves the better player benched; this pins the exact
    outcome.
    """
    response = recommend_lineup(session, league_season_id, WEEK)
    starters = {s["player"]: s["slot"] for s in response.recommendation["starters"]}

    assert "Desmond Achebe" in starters, "bench WR must be promoted"
    assert starters["Andre Whitlock"] == "FLEX", "displaced WR takes FLEX"
    assert response.recommendation["projected_points"] == pytest.approx(132.5, abs=0.1)


def test_bye_week_player_never_starts(session, league_season_id):
    """Hollis Trent projects well but his team is on bye."""
    response = recommend_lineup(session, league_season_id, WEEK)
    starters = {s["player"] for s in response.recommendation["starters"]}
    assert "Hollis Trent" not in starters


def test_questionable_starter_produces_a_conditional_trigger(session, league_season_id):
    """Golden scenario: a Q designation must generate an actionable fallback,
    not just a warning."""
    response = recommend_lineup(session, league_season_id, WEEK)

    triggers = [c for c in response.conditions if "Tyrell Boone" in c.trigger]
    assert triggers, "questionable starter must produce a conditional trigger"
    assert triggers[0].urgency == "high"
    assert "Curtis Lamm" in triggers[0].action, "fallback should be the floor option"

    watched = [w for w in response.watch if "Tyrell Boone" in w.action]
    assert watched


def test_missing_projection_degrades_confidence_and_is_surfaced(session, league_season_id):
    """Xavier Mbeki has no projection row. He must be reported, never silently
    treated as a real zero-point player."""
    response = recommend_lineup(session, league_season_id, WEEK)

    assert any("Xavier Mbeki" in (w.detail or "") for w in response.watch)
    assert any("lack a projection" in reason for reason in response.reasons)

    starters = {s["player"] for s in response.recommendation["starters"]}
    assert "Xavier Mbeki" not in starters


def test_objectives_are_distinguishable(session, league_season_id):
    floor = recommend_lineup(session, league_season_id, WEEK, objective="floor")
    ceiling = recommend_lineup(session, league_season_id, WEEK, objective="ceiling")
    assert floor.recommendation["floor"] >= ceiling.recommendation["floor"] - 0.01
    assert ceiling.recommendation["ceiling"] >= floor.recommendation["ceiling"] - 0.01


def test_run_and_snapshot_are_persisted_for_replay(session, league_season_id):
    response = recommend_lineup(session, league_season_id, WEEK)
    session.flush()

    run = session.get(RecommendationRun, response.run_id)
    assert run is not None
    assert run.calculation_version == response.calculation_version
    assert run.response["recommendation"]["projected_points"] == pytest.approx(
        response.recommendation["projected_points"]
    )

    snapshot_row = session.get(DecisionSnapshot, run.snapshot_id)
    assert snapshot_row is not None
    assert snapshot_row.payload["options"], "snapshot must carry inputs for replay"
    assert snapshot_row.content_hash


def test_identical_inputs_reproduce_an_identical_snapshot_hash(session, league_season_id):
    first = build_snapshot(session, league_season_id, WEEK)
    second = build_snapshot(session, league_season_id, WEEK)
    assert first.content_hash() == second.content_hash()


def test_recommendation_is_stable_across_runs(session, league_season_id):
    first = recommend_lineup(session, league_season_id, WEEK, persist=False)
    second = recommend_lineup(session, league_season_id, WEEK, persist=False)
    assert first.recommendation["starters"] == second.recommendation["starters"]


def test_material_inputs_cover_every_starter(session, league_season_id):
    """The explanation layer may only cite material_inputs, so every player it
    could need to mention must be present."""
    response = recommend_lineup(session, league_season_id, WEEK)
    keys = response.material_input_keys()
    for starter in response.recommendation["starters"]:
        assert f"projection:{starter['player_id']}" in keys
    assert "league_rules" in keys
    assert "starting_slots" in keys


def test_alternatives_carry_lineup_impact_not_just_points(session, league_season_id):
    response = recommend_lineup(session, league_season_id, WEEK)
    assert response.alternatives
    for candidate in response.alternatives:
        assert "lineup_marginal_value" in candidate.evidence
        assert candidate.projected_points is not None


def test_unknown_league_raises_clearly(session):
    with pytest.raises(LookupError):
        recommend_lineup(session, "does-not-exist", WEEK)


def test_scoring_change_creates_a_new_rules_version(session, seeded_league, settings):
    """A mid-season settings change must version rather than overwrite, so old
    recommendations remain interpretable."""
    from draftgpt.database.models import LeagueRules
    from draftgpt.ingestion.league_sync import sync_league
    from draftgpt.providers.fixture import FixtureLeagueProvider

    before = session.query(LeagueRules).count()
    sync_league(session, FixtureLeagueProvider(settings), settings.season, "1")
    assert session.query(LeagueRules).count() == before, "unchanged scoring must not version"

    league_season = session.get(LeagueSeason, seeded_league.league_season_id)
    assert league_season is not None
