"""End-to-end `/prep` against the fixture league, plus MCP tool wiring."""

from __future__ import annotations

import pytest

from draftgpt.commands.prep import load_context, prep_draft, prep_league, prep_player
from draftgpt.domain.contract import CommandResponse
from draftgpt.ingestion.identity import AmbiguousPlayerError


@pytest.fixture()
def prepped(session, settings, seeded_league):
    """Fixture league with season projections and ADP loaded."""
    from draftgpt.database.models import LeagueRules
    from draftgpt.evaluation.scoring import ScoringRules
    from draftgpt.ingestion.projections_sync import sync_adp, sync_projections
    from draftgpt.providers.fixture import FixtureProjectionProvider

    provider = FixtureProjectionProvider(settings)
    rules = session.query(LeagueRules).order_by(LeagueRules.version.desc()).first()
    scoring = ScoringRules.from_config(rules.scoring, f"{rules.id}:v{rules.version}")

    sync_projections(session, provider, settings.season, scope="season", rules=scoring)
    sync_adp(session, provider, settings.season)
    session.commit()
    return seeded_league


# --------------------------------------------------------------------------
# prep league
# --------------------------------------------------------------------------
def test_prep_league_returns_the_contract(session, seeded_league):
    response = prep_league(session)
    assert isinstance(response, CommandResponse)
    assert CommandResponse.model_validate(response.model_dump(mode="json"))
    assert response.command == "prep"
    assert response.subcommand == "league"


def test_prep_league_reads_the_actual_settings(session, seeded_league):
    league = prep_league(session).recommendation["league"]
    assert league["team_count"] == 12
    assert league["scoring_format"] == "PPR"
    assert league["is_superflex"] is False
    assert league["faab_budget"] == 100
    assert league["bench_slots"] == 6


def test_prep_league_computes_demand_from_the_lineup(session, seeded_league):
    demand = {
        row["position"]: row["starters_league_wide"]
        for row in prep_league(session).recommendation["positional_demand"]
    }
    # 12 teams x 1 QB, and WR/RB above their base counts because of the flex.
    assert demand["QB"] == pytest.approx(12.0, abs=0.5)
    assert demand["WR"] > 24
    assert demand["RB"] > 24


def test_prep_league_findings_carry_evidence(session, seeded_league):
    findings = prep_league(session).recommendation["findings"]
    assert findings
    for finding in findings:
        assert finding["evidence"], f"{finding['key']} has no evidence"
    assert any(f["key"] == "single_qb" for f in findings)
    assert any(f["key"] == "bonuses" for f in findings), "fixture league has bonuses"


# --------------------------------------------------------------------------
# prep draft
# --------------------------------------------------------------------------
def test_prep_draft_builds_a_board(session, prepped):
    rec = prep_draft(session, limit=25).recommendation
    assert rec["board"], "board should not be empty once projections are loaded"
    assert rec["total_ranked"] > 100
    assert len(rec["board"]) == 25


def test_board_is_sorted_by_value_over_replacement(session, prepped):
    board = prep_draft(session, limit=40).recommendation["board"]
    vors = [row["value_over_replacement"] for row in board]
    assert vors == sorted(vors, reverse=True)


def test_streaming_positions_do_not_top_the_board(session, prepped):
    """With sane projections, kickers and defenses must not outrank skill
    players. If they do, the projections are wrong."""
    board = prep_draft(session, limit=24).recommendation["board"]
    assert not [row for row in board if row["position"] in {"K", "DST"}]


def test_position_filter_narrows_the_same_board(session, prepped):
    full = prep_draft(session, limit=200).recommendation["board"]
    only_rb = prep_draft(session, position="RB", limit=200).recommendation["board"]

    assert all(row["position"] == "RB" for row in only_rb)
    ranks_in_full = {r["player_id"]: r["overall_rank"] for r in full}
    for row in only_rb:
        if row["player_id"] in ranks_in_full:
            assert row["overall_rank"] == ranks_in_full[row["player_id"]], (
                "filtered view must not renumber players"
            )


def test_scarcity_reports_every_started_position(session, prepped):
    scarcity = prep_draft(session).recommendation["scarcity"]
    positions = {row["position"] for row in scarcity}
    assert {"QB", "RB", "WR", "TE", "K", "DST"} <= positions
    for row in scarcity:
        assert row["starters_needed_league_wide"] > 0


def test_availability_requires_a_pick_number(session, prepped):
    assert prep_draft(session).recommendation["availability_at_pick"] is None
    with_pick = prep_draft(session, at_pick=20, limit=10).recommendation
    assert with_pick["availability_at_pick"]
    for row in with_pick["availability_at_pick"]:
        assert 0.0 <= row["survival_probability"] <= 1.0


def test_later_picks_reduce_survival(session, prepped):
    early = {
        r["player"]: r["survival_probability"]
        for r in prep_draft(session, at_pick=5, limit=15).recommendation["availability_at_pick"]
    }
    late = {
        r["player"]: r["survival_probability"]
        for r in prep_draft(session, at_pick=60, limit=15).recommendation["availability_at_pick"]
    }
    shared = set(early) & set(late)
    assert shared
    assert all(late[name] <= early[name] for name in shared)


def test_roster_plan_covers_the_roster(session, prepped):
    plan = prep_draft(session).recommendation["roster_plan"]
    assert plan
    assert any(row["position"] == "RB" for row in plan)
    assert any("streaming" in row["note"] for row in plan if row["position"] in {"K", "DST"})


def test_board_without_projections_warns_instead_of_failing(session, seeded_league):
    """seeded_league loads weekly projections only, so the season board is empty."""
    rec = prep_draft(session).recommendation
    assert rec["board"] == []
    assert any("no season projections" in w for w in rec["warnings"])


# --------------------------------------------------------------------------
# prep player
# --------------------------------------------------------------------------
def test_prep_player_prices_a_single_player(session, prepped):
    rec = prep_player(session, "Marcus Deveraux").recommendation
    assert rec["player"] == "Marcus Deveraux"
    assert rec["position"] == "RB"
    assert rec["projected_points"] > 0
    assert rec["position_rank"].startswith("RB")
    assert rec["positional_neighbours"]


def test_prep_player_reports_survival_at_a_pick(session, prepped):
    response = prep_player(session, "Marcus Deveraux", at_pick=30)
    probability = response.recommendation["survival_probability_at_pick"]
    assert probability is not None
    assert 0.0 <= probability <= 1.0
    assert any("still there at pick 30" in reason for reason in response.reasons)


def test_prep_player_refuses_ambiguous_names(session, prepped):
    """Two players sharing a name must not be silently resolved."""
    from draftgpt.database.models import Player
    from draftgpt.ingestion.identity import normalize_name

    for _ in range(2):
        session.add(
            Player(
                full_name="Duplicate Name",
                normalized_name=normalize_name("Duplicate Name"),
                primary_position="WR",
            )
        )
    session.flush()

    with pytest.raises(AmbiguousPlayerError):
        prep_player(session, "Duplicate Name")


def test_prep_player_unknown_name_raises_lookup(session, prepped):
    with pytest.raises(LookupError):
        prep_player(session, "Nobody Whatsoever")


def test_prep_player_without_projection_says_so(session, prepped):
    """Xavier Mbeki is deliberately projection-less in the fixture."""
    response = prep_player(session, "Xavier Mbeki")
    assert "no projection" in response.recommendation["note"]
    assert response.confidence == 0.0


def test_load_context_requires_a_synced_league(session):
    with pytest.raises(LookupError):
        load_context(session)


# --------------------------------------------------------------------------
# MCP wiring
# --------------------------------------------------------------------------
def test_mcp_tool_definitions_are_wellformed():
    from draftgpt.interfaces.mcp_server import TOOL_DEFINITIONS

    names = {d["name"] for d in TOOL_DEFINITIONS}
    assert {"prep_league", "prep_draft_board", "prep_player", "league_status"} <= names

    for definition in TOOL_DEFINITIONS:
        assert definition["description"].strip()
        assert definition["schema"]["type"] == "object"
        assert callable(definition["fn"])
        for prop in definition["schema"].get("properties", {}).values():
            assert "description" in prop, "each argument needs a description for the model"


def test_mcp_instructions_state_the_read_only_league_scope():
    from draftgpt.interfaces.mcp_server import SERVER_INSTRUCTIONS

    # Collapse whitespace: the instructions are hard-wrapped, so phrases span
    # line breaks.
    lowered = " ".join(SERVER_INSTRUCTIONS.lower().split())
    assert "read-only" in lowered
    assert "freshness" in lowered
    assert "do not assert any fact that is not present in the tool output" in lowered
