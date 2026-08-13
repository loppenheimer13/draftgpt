"""Deterministic fixture generation and seeding.

Produces a complete, self-consistent 12-team PPR league with weekly projections
so the whole pipeline runs with no credentials and no network. Everything is
derived from a fixed seed, so golden-scenario tests are stable.

The owner's roster is hand-authored to contain the situations that actually
exercise the optimizer rather than random noise:

* a **FLEX displacement**: a WR-only player who must push a WR out of FLEX,
  which a naive "best player to most restrictive slot" greedy gets wrong;
* a **questionable starter** with both a safe floor and a high-ceiling
  alternative on the bench, exercising conditional triggers;
* a **bye-week starter**, which must be excluded rather than scored as zero;
* a **missing projection**, which must degrade confidence rather than crash.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

SEASON = 2026
WEEK = 1
TEAM_COUNT = 12
OWNER_TEAM_ID = "1"

#: PPR with a modest passing-yard rate. Canonical stat keys throughout.
SCORING: dict[str, Any] = {
    "pass_yd": 0.04,
    "pass_td": 4.0,
    "pass_int": -2.0,
    "rush_yd": 0.1,
    "rush_td": 6.0,
    "rec": 1.0,
    "rec_yd": 0.1,
    "rec_td": 6.0,
    "fum_lost": -2.0,
    "bonuses": [
        {"stat": "pass_yd", "threshold": 300, "points": 3.0},
        {"stat": "rush_yd", "threshold": 100, "points": 3.0},
        {"stat": "rec_yd", "threshold": 100, "points": 3.0},
    ],
}

ROSTER_SLOTS: list[dict[str, Any]] = [
    {"slot": "QB", "count": 1, "eligible_positions": ["QB"], "is_starting": True},
    {"slot": "RB", "count": 2, "eligible_positions": ["RB"], "is_starting": True},
    {"slot": "WR", "count": 2, "eligible_positions": ["WR"], "is_starting": True},
    {"slot": "TE", "count": 1, "eligible_positions": ["TE"], "is_starting": True},
    {"slot": "FLEX", "count": 1, "eligible_positions": ["RB", "WR", "TE"], "is_starting": True},
    {"slot": "K", "count": 1, "eligible_positions": ["K"], "is_starting": True},
    {"slot": "DST", "count": 1, "eligible_positions": ["DST"], "is_starting": True},
    {"slot": "BE", "count": 6, "eligible_positions": [], "is_starting": False},
]

# (external_id, name, position, team, slot, median, floor, ceiling, designation)
OWNER_ROSTER: list[tuple[str, str, str, str, str, float, float, float, str]] = [
    ("p001", "Jalen Carter-Reid",  "QB",  "PHI", "QB",   21.4, 14.2, 31.0, ""),
    ("p002", "Marcus Deveraux",    "RB",  "SF",  "RB",   18.9, 10.1, 30.2, ""),
    # Questionable starter: strong median, but the bench holds both a safe
    # floor and a ceiling swing -- this drives the conditional trigger.
    ("p003", "Tyrell Boone",       "RB",  "DET", "RB",   15.2,  6.0, 27.5, "Q"),
    ("p004", "Andre Whitlock",     "WR",  "MIN", "WR",   17.6, 11.0, 26.4, ""),
    ("p005", "Kai Ostrander",      "WR",  "CIN", "WR",   14.1,  8.2, 22.9, ""),
    ("p006", "Bennett Shaw",       "TE",  "KC",  "TE",   12.7,  7.4, 19.8, ""),
    # FLEX displacement: outscores the WR2 above, so the optimal solution must
    # move Ostrander to FLEX and promote this player into the WR slot.
    ("p007", "Desmond Achebe",     "WR",  "BUF", "BE",   16.3,  9.8, 25.1, ""),
    ("p008", "Rory Nakamura",      "K",   "BAL", "K",     8.4,  5.0, 13.0, ""),
    ("p009", "Seattle",            "DST", "SEA", "DST",   7.9,  2.0, 16.0, ""),
    # Safe floor alternative for the questionable RB.
    ("p010", "Curtis Lamm",        "RB",  "GB",  "BE",   11.8,  8.9, 15.2, ""),
    # Ceiling swing alternative.
    ("p011", "Emeka Sowande",      "RB",  "LAC", "BE",   10.9,  3.1, 24.8, ""),
    # On bye: must be excluded from the lineup, not merely scored low.
    ("p012", "Hollis Trent",       "WR",  "DAL", "BE",   13.5,  8.0, 21.0, ""),
    # No projection row is emitted for this player on purpose.
    ("p013", "Xavier Mbeki",       "TE",  "NYJ", "BE",    0.0,  0.0,  0.0, ""),
    ("p014", "Grant Ellery",       "QB",  "ARI", "BE",   16.1, 10.0, 24.0, ""),
    ("p015", "Damon Fitzhugh",     "WR",  "TB",  "BE",    9.2,  4.5, 16.1, ""),
]

#: Dallas is on bye in the fixture week, which strands p012.
BYE_TEAMS: dict[str, int] = {"DAL": WEEK}

#: Players with no projection row -- exercises the degraded-confidence path.
NO_PROJECTION = {"p013"}

FILLER_POSITIONS = ["QB", "RB", "RB", "WR", "WR", "TE", "K", "DST", "RB", "WR", "WR", "TE"]
FILLER_TEAMS = ["ATL", "CHI", "DEN", "HOU", "IND", "JAX", "LV", "MIA", "NE", "NO", "PIT", "TEN"]


def _filler_players() -> list[dict[str, Any]]:
    """Deterministic opponents' rosters and the free-agent pool."""
    players: list[dict[str, Any]] = []
    index = 0
    for team_number in range(2, TEAM_COUNT + 1):
        for slot_index, position in enumerate(FILLER_POSITIONS):
            index += 1
            players.append(
                {
                    "player_external_id": f"f{index:04d}",
                    "name": f"Filler {position}{index:04d}",
                    "position": position,
                    "team": FILLER_TEAMS[(index + slot_index) % len(FILLER_TEAMS)],
                    "owner_team": str(team_number),
                    # Deterministic descending value, no randomness.
                    "median": round(20.0 - (index % 17) * 0.9, 2),
                }
            )
    for extra in range(1, 61):
        players.append(
            {
                "player_external_id": f"fa{extra:04d}",
                "name": f"FreeAgent {extra:04d}",
                "position": FILLER_POSITIONS[extra % len(FILLER_POSITIONS)],
                "team": FILLER_TEAMS[extra % len(FILLER_TEAMS)],
                "owner_team": None,
                "median": round(9.0 - (extra % 9) * 0.7, 2),
            }
        )
    return players


def build_fixture_files(target: Path) -> dict[str, Path]:
    """Write the fixture set. Idempotent and deterministic."""
    target.mkdir(parents=True, exist_ok=True)
    filler = _filler_players()

    settings = {
        "external_id": "fixture-league-1",
        "name": "Fixture Dynasty of Testing",
        "season": SEASON,
        "team_count": TEAM_COUNT,
        "current_week": WEEK,
        "regular_season_weeks": 14,
        "playoff_team_count": 6,
        "playoff_weeks": [15, 16, 17],
        "status": "in_season",
        "format": "redraft",
        "scoring": SCORING,
        "roster_slots": ROSTER_SLOTS,
        "roster_size": sum(s["count"] for s in ROSTER_SLOTS),
        "ir_slots": 0,
        "waiver_type": "faab",
        "faab_budget": 100,
        "waiver_process_days": ["WED"],
        "trade_deadline_at": None,
        "raw_settings": {"note": "hand-authored fixture; no external provider"},
    }

    owner_players = [
        {
            "player_external_id": pid,
            "name": name,
            "position": position,
            "pro_team": team,
            "slot": slot,
            "injury_status": designation or "ACTIVE",
            "acquisition_type": "draft",
        }
        for pid, name, position, team, slot, _, _, _, designation in OWNER_ROSTER
    ]

    rosters: list[dict[str, Any]] = [
        {
            "team_external_id": OWNER_TEAM_ID,
            "team_name": "The Reproducible Snapshots",
            "wins": 0, "losses": 0, "ties": 0,
            "points_for": 0.0, "points_against": 0.0,
            "faab_remaining": 100, "waiver_priority": 6, "draft_position": 6,
            "players": owner_players,
        }
    ]
    for team_number in range(2, TEAM_COUNT + 1):
        team_players = [p for p in filler if p["owner_team"] == str(team_number)]
        rosters.append(
            {
                "team_external_id": str(team_number),
                "team_name": f"Fixture Team {team_number}",
                "wins": 0, "losses": 0, "ties": 0,
                "points_for": 0.0, "points_against": 0.0,
                "faab_remaining": 100,
                "waiver_priority": team_number,
                "draft_position": team_number,
                "players": [
                    {
                        "player_external_id": p["player_external_id"],
                        "name": p["name"],
                        "position": p["position"],
                        "pro_team": p["team"],
                        "slot": "BE",
                        "injury_status": "ACTIVE",
                        "acquisition_type": "draft",
                    }
                    for p in team_players
                ],
            }
        )

    all_players = [
        {
            "player_external_id": pid,
            "name": name,
            "first_name": name.split()[0],
            "last_name": name.split()[-1],
            "position": position,
            "positions": [position],
            "pro_team": team,
            "injury_status": designation or "ACTIVE",
            "is_team_defense": position == "DST",
        }
        for pid, name, position, team, _, _, _, _, designation in OWNER_ROSTER
    ] + [
        {
            "player_external_id": p["player_external_id"],
            "name": p["name"],
            "first_name": p["name"].split()[0],
            "last_name": p["name"].split()[-1],
            "position": p["position"],
            "positions": [p["position"]],
            "pro_team": p["team"],
            "injury_status": "ACTIVE",
            "is_team_defense": p["position"] == "DST",
        }
        for p in filler
    ]

    paths = {
        "league_settings": target / "league_settings.json",
        "league_rosters": target / "league_rosters.json",
        "players": target / "players.json",
        "projections_weekly": target / "projections_weekly.csv",
    }
    paths["league_settings"].write_text(json.dumps(settings, indent=2) + "\n")
    paths["league_rosters"].write_text(json.dumps(rosters, indent=2) + "\n")
    paths["players"].write_text(json.dumps(all_players, indent=2) + "\n")

    _write_projections(paths["projections_weekly"], filler)
    return paths


def _write_projections(path: Path, filler: list[dict[str, Any]]) -> None:
    """Emit projections as canonical stat lines, not pre-scored points.

    Stat lines are back-solved from the intended median so the fixture exercises
    the real scoring engine rather than bypassing it.
    """
    columns = [
        "player_external_id", "name", "position", "team", "season", "week", "scope",
        "rec", "rec_yd", "rec_td", "rush_yd", "rush_td", "pass_yd", "pass_td", "pass_int",
        "floor_points", "ceiling_points",
    ]
    rows: list[dict[str, Any]] = []

    for pid, name, position, team, _slot, median, floor, ceiling, _d in OWNER_ROSTER:
        if pid in NO_PROJECTION:
            continue
        rows.append(
            {
                "player_external_id": pid, "name": name, "position": position, "team": team,
                "season": SEASON, "week": WEEK, "scope": "week",
                **_stat_line(position, median),
                "floor_points": floor, "ceiling_points": ceiling,
            }
        )

    for player in filler:
        median = player["median"]
        rows.append(
            {
                "player_external_id": player["player_external_id"],
                "name": player["name"],
                "position": player["position"],
                "team": player["team"],
                "season": SEASON, "week": WEEK, "scope": "week",
                **_stat_line(player["position"], median),
                "floor_points": round(median * 0.6, 2),
                "ceiling_points": round(median * 1.6, 2),
            }
        )

    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, restval="")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _stat_line(position: str, median: float) -> dict[str, float]:
    """Back-solve a plausible stat line that scores to ``median`` under SCORING.

    Kept intentionally simple and threshold-free so the fixture's expected
    points are exact -- bonuses would make the inverse non-linear.
    """
    median = max(median, 0.0)
    if position == "QB":
        # 0.04/yd + 4/td: 250 yards = 10 pts, remainder as TDs.
        yards = 240.0
        td = max(0.0, (median - yards * 0.04) / 4.0)
        return {"pass_yd": yards, "pass_td": round(td, 3), "pass_int": 0.0}
    if position == "RB":
        # 3 receptions + rushing yards, remainder as rushing TDs.
        rec, rec_yd = 3.0, 20.0
        base = rec * 1.0 + rec_yd * 0.1
        rush_yd = 60.0
        td = max(0.0, (median - base - rush_yd * 0.1) / 6.0)
        return {"rec": rec, "rec_yd": rec_yd, "rush_yd": rush_yd, "rush_td": round(td, 3)}
    if position in {"WR", "TE"}:
        rec = 5.0
        rec_yd = 60.0
        base = rec * 1.0 + rec_yd * 0.1
        td = max(0.0, (median - base) / 6.0)
        return {"rec": rec, "rec_yd": rec_yd, "rec_td": round(td, 3)}
    # K and DST have no modelled stat line in the fixture; carry points directly
    # via rushing yards so the scoring engine still produces the intended total.
    return {"rush_yd": round(median / 0.1, 2)}


def expected_median(position: str, median: float) -> float:
    """The exact points ``_stat_line`` should score. Used by tests."""
    from draftgpt.evaluation.scoring import ScoringRules, score_stat_line

    return score_stat_line(_stat_line(position, median), ScoringRules.from_config(SCORING))
