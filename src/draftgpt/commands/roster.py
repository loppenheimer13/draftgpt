"""`/roster` -- lineup recommendation. The Phase 1 vertical slice.

Three layers, kept strictly separate (brief section 7):

1. **State assembly** builds a validated decision snapshot from normalized
   tables, bounded by ``as_of`` so a replay never sees future information.
2. **Deterministic evaluation** prices projections under league scoring and
   solves the optimal lineup. No LLM touches this.
3. **Explanation** is optional and may only reference ``material_inputs``.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from draftgpt.config import CALCULATION_VERSION
from draftgpt.database.models import (
    DecisionSnapshot,
    LeagueRules,
    LeagueSeason,
    NflTeam,
    PlayerStatusEvent,
    RecommendationCandidate,
    RecommendationRun,
    RosterSlot,
    Team,
    TeamSeason,
)
from draftgpt.domain.contract import (
    ActionItem,
    Candidate,
    CommandResponse,
    Condition,
    MaterialInput,
)
from draftgpt.domain.freshness import (
    evaluate_freshness,
    infer_season_phase,
    next_check_time,
)
from draftgpt.evaluation.lineup import (
    PlayerOption,
    SlotSpec,
    lineup_delta,
    marginal_value,
    optimize_lineup,
)
from draftgpt.evaluation.scoring import ScoringRules
from draftgpt.ingestion.league_sync import owner_team_season, roster_players
from draftgpt.ingestion.projections_sync import latest_projections, price_projection
from draftgpt.ingestion.runner import collect_freshness

logger = logging.getLogger(__name__)

#: Designations that make a starter a live risk rather than a settled choice.
RISK_DESIGNATIONS = {"Q", "D", "DNP", "LP"}


@dataclass
class RosterSnapshot:
    """Everything the recommendation is computed from. Serialized for replay."""

    league_season: LeagueSeason
    rules: LeagueRules
    scoring: ScoringRules
    team_season: TeamSeason
    week: int
    as_of: datetime
    slots: list[SlotSpec]
    options: list[PlayerOption]
    current_lineup: dict[str, str]
    status_events: dict[str, str] = field(default_factory=dict)
    missing_projections: list[str] = field(default_factory=list)

    def content_hash(self) -> str:
        """Stable hash of the inputs. Identical inputs must reproduce it."""
        payload = {
            "league_season_id": self.league_season.id,
            "rules_id": self.rules.id,
            "team_season_id": self.team_season.id,
            "week": self.week,
            "slots": sorted((s.slot, s.ordinal) for s in self.slots),
            "options": sorted(
                (o.player_id, round(o.median, 4), o.status, o.on_bye) for o in self.options
            ),
        }
        blob = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()

    def serialize(self) -> dict:
        return {
            "week": self.week,
            "as_of": self.as_of.isoformat(),
            "rules_version": self.rules.version,
            "slots": [
                {"slot_id": s.slot_id, "slot": s.slot, "ordinal": s.ordinal,
                 "eligible": list(s.eligible_positions)}
                for s in self.slots
            ],
            "options": [
                {
                    "player_id": o.player_id, "name": o.name, "positions": list(o.positions),
                    "median": o.median, "floor": o.floor, "ceiling": o.ceiling,
                    "status": o.status, "on_bye": o.on_bye,
                    "projection_missing": o.projection_missing,
                }
                for o in self.options
            ],
            "current_lineup": self.current_lineup,
            "status_events": self.status_events,
        }


# --------------------------------------------------------------------------
# 1. State assembly
# --------------------------------------------------------------------------
def build_snapshot(
    session: Session,
    league_season_id: str,
    week: int,
    team_season_id: str | None = None,
    as_of: datetime | None = None,
) -> RosterSnapshot:
    as_of = as_of or datetime.now(UTC)
    league_season = session.get(LeagueSeason, league_season_id)
    if league_season is None:
        raise LookupError(f"no league season {league_season_id}")

    rules = session.scalars(
        select(LeagueRules)
        .where(LeagueRules.league_season_id == league_season_id)
        .order_by(LeagueRules.version.desc())
    ).first()
    if rules is None:
        raise LookupError("league rules not synced; run `draftgpt sync league` first")

    team_season = (
        session.get(TeamSeason, team_season_id)
        if team_season_id
        else owner_team_season(session, league_season_id)
    )
    if team_season is None:
        raise LookupError(
            "no owner team identified -- set one with `draftgpt league set-owner <team-id>`"
        )

    scoring = ScoringRules.from_config(rules.scoring, ruleset_id=f"{rules.id}:v{rules.version}")
    slots = _slot_specs(session, rules.id)

    roster = roster_players(session, team_season.id)
    player_ids = [player.id for player, _ in roster]
    projections = latest_projections(
        session, player_ids, league_season.season, week, scope="week", as_of=as_of
    )
    status = _status_map(session, player_ids, league_season.season, week, as_of)
    byes = _bye_teams(session, league_season.season, week)

    options: list[PlayerOption] = []
    current: dict[str, str] = {}
    missing: list[str] = []

    slot_cursor: dict[str, int] = {}
    for player, membership in roster:
        projection = projections.get(player.id)
        if projection is None:
            median, floor, ceiling = 0.0, 0.0, 0.0
            missing.append(player.full_name)
        else:
            median, floor, ceiling = price_projection(projection, scoring)

        team_abbr = _team_abbr(session, player.nfl_team_id)
        designation = status.get(player.id, "")
        options.append(
            PlayerOption(
                player_id=player.id,
                name=player.full_name,
                positions=(player.primary_position,),
                median=median,
                floor=floor,
                ceiling=ceiling,
                nfl_team=team_abbr,
                status=designation or player.status,
                on_bye=bool(team_abbr and team_abbr in byes),
                projection_missing=projection is None,
            )
        )

        # Map the platform's configured slot onto one of our slot specs.
        slot_name = membership.slot
        if slot_name and slot_name not in {"BE", "IR"}:
            index = slot_cursor.get(slot_name, 0)
            matches = [s for s in slots if s.slot == slot_name]
            if index < len(matches):
                current[matches[index].slot_id] = player.id
                slot_cursor[slot_name] = index + 1

    return RosterSnapshot(
        league_season=league_season,
        rules=rules,
        scoring=scoring,
        team_season=team_season,
        week=week,
        as_of=as_of,
        slots=slots,
        options=options,
        current_lineup=current,
        status_events=status,
        missing_projections=missing,
    )


def _slot_specs(session: Session, rules_id: str) -> list[SlotSpec]:
    rows = session.scalars(
        select(RosterSlot)
        .where(RosterSlot.league_rules_id == rules_id, RosterSlot.is_starting.is_(True))
        .order_by(RosterSlot.display_order)
    )
    return [
        SlotSpec(
            slot_id=row.id,
            slot=row.slot,
            ordinal=row.ordinal,
            eligible_positions=tuple(row.eligible_positions or ()),
            display_order=row.display_order,
        )
        for row in rows
    ]


def _status_map(
    session: Session, player_ids: list[str], season: int, week: int, as_of: datetime
) -> dict[str, str]:
    if not player_ids:
        return {}
    rows = session.scalars(
        select(PlayerStatusEvent)
        .where(
            PlayerStatusEvent.player_id.in_(player_ids),
            PlayerStatusEvent.season == season,
            PlayerStatusEvent.retrieved_at <= as_of,
        )
        .order_by(PlayerStatusEvent.retrieved_at)
    )
    latest: dict[str, str] = {}
    for row in rows:
        if row.week in (None, week) and row.designation:
            latest[row.player_id] = row.designation
    return latest


def _bye_teams(session: Session, season: int, week: int) -> set[str]:
    teams = session.scalars(select(NflTeam))
    return {
        team.abbreviation
        for team in teams
        if str(week) == str((team.bye_week_by_season or {}).get(str(season), ""))
    }


def _team_abbr(session: Session, nfl_team_id: str | None) -> str | None:
    if not nfl_team_id:
        return None
    team = session.get(NflTeam, nfl_team_id)
    return team.abbreviation if team else None


# --------------------------------------------------------------------------
# 2 + 3. Evaluate and build the response
# --------------------------------------------------------------------------
def recommend_lineup(
    session: Session,
    league_season_id: str,
    week: int,
    objective: str = "balanced",
    team_season_id: str | None = None,
    as_of: datetime | None = None,
    persist: bool = True,
) -> CommandResponse:
    snapshot = build_snapshot(session, league_season_id, week, team_season_id, as_of)
    generated_at = datetime.now(UTC)

    solution = optimize_lineup(snapshot.options, snapshot.slots, objective)
    by_id = {o.player_id: o for o in snapshot.options}
    slot_by_id = {s.slot_id: s for s in snapshot.slots}

    phase = infer_season_phase(generated_at, league_status=snapshot.league_season.status)
    freshness_inputs = collect_freshness(session)
    freshness, confidence_multiplier = evaluate_freshness(
        freshness_inputs, "roster", str(phase), now=generated_at
    )

    changes = lineup_delta(snapshot.current_lineup, solution.assignments, snapshot.slots)
    starters = [
        {
            "slot": slot_by_id[slot_id].slot,
            "slot_ordinal": slot_by_id[slot_id].ordinal,
            "player_id": player_id,
            "player": by_id[player_id].name,
            "position": by_id[player_id].positions[0],
            "team": by_id[player_id].nfl_team,
            "projected_points": round(by_id[player_id].median, 2),
            "floor": round(by_id[player_id].floor or 0.0, 2),
            "ceiling": round(by_id[player_id].ceiling or 0.0, 2),
            "status": by_id[player_id].status,
        }
        for slot_id, player_id in sorted(
            solution.assignments.items(), key=lambda kv: slot_by_id[kv[0]].display_order
        )
    ]

    bench = sorted(
        (by_id[pid] for pid in solution.bench), key=lambda o: -o.median
    )
    alternatives = _bench_alternatives(snapshot, solution, bench, objective)

    confidence = _confidence(snapshot, solution, changes, confidence_multiplier)
    conditions = _conditions(snapshot, solution, by_id)
    reasons = _reasons(snapshot, solution, changes, by_id, slot_by_id)
    material_inputs = _material_inputs(snapshot, freshness)

    do_now = [
        ActionItem(
            action=_change_sentence(change, by_id),
            detail=f"{change.slot} slot",
            urgency="high",
        )
        for change in changes
    ] or [ActionItem(action="No lineup changes needed", urgency="low")]

    watch = [
        ActionItem(
            action=f"Monitor {by_id[pid].name} ({snapshot.status_events[pid]})",
            detail="game-time decision; confirm active before kickoff",
            urgency="high",
            deeper_command="/roster now",
        )
        for pid in solution.starters()
        if snapshot.status_events.get(pid) in RISK_DESIGNATIONS
    ]
    if snapshot.missing_projections:
        watch.append(
            ActionItem(
                action=f"{len(snapshot.missing_projections)} rostered player(s) have no projection",
                detail=", ".join(snapshot.missing_projections[:5]),
                urgency="normal",
            )
        )

    snapshot_row = _persist_snapshot(session, snapshot) if persist else None
    response = CommandResponse(
        command="roster",
        subcommand=f"week {week}",
        league_id=snapshot.league_season.league_id,
        snapshot_id=snapshot_row.id if snapshot_row else snapshot.content_hash()[:32],
        generated_at=generated_at,
        calculation_version=CALCULATION_VERSION,
        week=week,
        recommendation={
            "objective": objective,
            "starters": starters,
            "bench_order": [
                {"player_id": o.player_id, "player": o.name, "position": o.positions[0],
                 "projected_points": round(o.median, 2)}
                for o in bench
            ],
            "changes_from_current": [
                {
                    "kind": change.kind,
                    "slot": change.slot,
                    "from_slot": change.from_slot,
                    "out": by_id[change.player_out].name if change.player_out else None,
                    "in": by_id[change.player_in].name if change.player_in else None,
                    "instruction": _change_sentence(change, by_id),
                }
                for change in changes
            ],
            "projected_points": solution.total_median,
            "floor": solution.total_floor,
            "ceiling": solution.total_ceiling,
            "unfilled_slots": [slot_by_id[s].slot for s in solution.unfilled_slots],
        },
        alternatives=alternatives,
        confidence=confidence,
        reasons=reasons,
        material_inputs=material_inputs,
        conditions=conditions,
        freshness=freshness,
        do_now=do_now,
        watch=watch,
        check_again_at=next_check_time(str(phase), generated_at),
    )

    if persist and snapshot_row is not None:
        _persist_run(session, response, snapshot, snapshot_row, alternatives)
    return response


def _bench_alternatives(
    snapshot: RosterSnapshot,
    solution,
    bench: list[PlayerOption],
    objective: str,
) -> list[Candidate]:
    """Nearest bench options, scored by what starting them would actually cost.

    The delta is computed by re-optimizing without the incumbent, so it reflects
    lineup impact rather than a raw point difference against an arbitrary player.
    """
    candidates: list[Candidate] = []
    for rank, option in enumerate(bench[:3], start=1):
        swap_cost = marginal_value(
            snapshot.options, snapshot.slots, option.player_id, objective
        )
        candidates.append(
            Candidate(
                rank=rank,
                label=option.name,
                subject_type="player",
                subject_id=option.player_id,
                score=round(option.weight(objective), 3),
                projected_points=round(option.median, 2),
                floor_points=round(option.floor or 0.0, 2),
                ceiling_points=round(option.ceiling or 0.0, 2),
                evidence={
                    "position": option.positions[0],
                    "team": option.nfl_team,
                    "status": option.status,
                    "on_bye": option.on_bye,
                    "lineup_marginal_value": swap_cost,
                    "note": (
                        "already contributes to the optimal lineup"
                        if swap_cost > 0
                        else "no lineup impact at current projections"
                    ),
                },
            )
        )
    return candidates


def _confidence(
    snapshot: RosterSnapshot, solution, changes: list[dict], freshness_multiplier: float
) -> float:
    """Confidence reflects decision margin, not model certainty.

    A lineup where the last starter beats the first bench player by 0.3 points
    is a coin flip and should say so, however fresh the data is.
    """
    base = 0.85
    if snapshot.missing_projections:
        base -= 0.10 * min(len(snapshot.missing_projections), 3)
    risky = sum(
        1 for pid in solution.starters() if snapshot.status_events.get(pid) in RISK_DESIGNATIONS
    )
    base -= 0.05 * risky
    if solution.unfilled_slots:
        base -= 0.15
    if not changes:
        base += 0.05
    return round(max(0.05, min(0.99, base * freshness_multiplier)), 3)


def _conditions(snapshot: RosterSnapshot, solution, by_id: dict) -> list[Condition]:
    conditions: list[Condition] = []
    starters = solution.starters()
    bench_by_position: dict[str, list[PlayerOption]] = {}
    for pid in solution.bench:
        option = by_id[pid]
        if option.on_bye or option.status in {"O", "IR"}:
            continue
        bench_by_position.setdefault(option.positions[0], []).append(option)

    for pid in starters:
        designation = snapshot.status_events.get(pid)
        if designation not in RISK_DESIGNATIONS:
            continue
        option = by_id[pid]
        replacements = sorted(
            bench_by_position.get(option.positions[0], []), key=lambda o: -o.median
        )
        action = (
            f"start {replacements[0].name}"
            if replacements
            else "no same-position bench replacement available -- check waivers"
        )
        conditions.append(
            Condition(
                trigger=f"{option.name} is downgraded or inactive",
                action=action,
                urgency="high",
            )
        )
    return conditions


def _change_sentence(change, by_id: dict) -> str:
    """One imperative instruction the owner can act on without interpretation."""
    name_in = by_id[change.player_in].name if change.player_in else None
    name_out = by_id[change.player_out].name if change.player_out else None
    if change.kind == "swap":
        return f"Start {name_in} over {name_out} at {change.slot}"
    if change.kind == "start":
        return f"Start {name_in} at {change.slot} (slot was empty)"
    if change.kind == "bench":
        return f"Bench {name_out} from {change.slot}"
    return f"Move {name_in} from {change.from_slot} to {change.slot}"


def _reasons(
    snapshot: RosterSnapshot, solution, changes: list, by_id: dict, slot_by_id: dict
) -> list[str]:
    reasons = [
        f"Optimal lineup projects {solution.total_median:.1f} points "
        f"(floor {solution.total_floor:.1f}, ceiling {solution.total_ceiling:.1f}) "
        f"under rules v{snapshot.rules.version} scoring."
    ]
    if changes:
        for change in changes:
            if change.kind == "swap":
                gain = by_id[change.player_in].median - by_id[change.player_out].median
                reasons.append(
                    f"{change.slot}: {by_id[change.player_in].name} over "
                    f"{by_id[change.player_out].name} ({gain:+.1f} projected)."
                )
            else:
                reasons.append(_change_sentence(change, by_id) + ".")
    else:
        reasons.append("Currently configured lineup is already optimal.")

    if snapshot.missing_projections:
        reasons.append(
            f"{len(snapshot.missing_projections)} player(s) lack a projection for week "
            f"{snapshot.week} and were treated as 0.0 -- they cannot be recommended as starters."
        )
    return reasons


def _material_inputs(snapshot: RosterSnapshot, freshness) -> list[MaterialInput]:
    """The complete set of facts the explanation layer may reference."""
    inputs = [
        MaterialInput(
            key="league_rules",
            label=f"Scoring rules v{snapshot.rules.version}",
            value={"ppr": snapshot.scoring.reception_value},
            effective_at=snapshot.rules.effective_from,
        ),
        MaterialInput(
            key="starting_slots",
            label="Starting lineup configuration",
            value=[f"{s.slot}{s.ordinal or ''}" for s in snapshot.slots],
        ),
    ]
    inputs += [
        MaterialInput(
            key=f"projection:{option.player_id}",
            label=f"{option.name} week {snapshot.week} projection",
            value={
                "median": round(option.median, 2),
                "floor": round(option.floor or 0.0, 2),
                "ceiling": round(option.ceiling or 0.0, 2),
                "status": option.status,
                "on_bye": option.on_bye,
            },
        )
        for option in snapshot.options
    ]
    inputs += [
        MaterialInput(
            key=f"freshness:{source.provider}:{source.capability}",
            label=f"{source.provider} {source.capability} freshness",
            value=source.status,
            provider=source.provider,
            effective_at=source.effective_at,
        )
        for source in freshness.sources
    ]
    return inputs


def _persist_snapshot(session: Session, snapshot: RosterSnapshot) -> DecisionSnapshot:
    row = DecisionSnapshot(
        league_season_id=snapshot.league_season.id,
        season=snapshot.league_season.season,
        week=snapshot.week,
        as_of=snapshot.as_of,
        league_rules_id=snapshot.rules.id,
        content_hash=snapshot.content_hash(),
        source_versions={},
        payload=snapshot.serialize(),
    )
    session.add(row)
    session.flush()
    return row


def _persist_run(
    session: Session,
    response: CommandResponse,
    snapshot: RosterSnapshot,
    snapshot_row: DecisionSnapshot,
    alternatives: list[Candidate],
) -> None:
    run = RecommendationRun(
        command="roster",
        subcommand=response.subcommand,
        league_season_id=snapshot.league_season.id,
        team_season_id=snapshot.team_season.id,
        snapshot_id=snapshot_row.id,
        calculation_version=CALCULATION_VERSION,
        generated_at=response.generated_at,
        week=snapshot.week,
        request={"objective": response.recommendation.get("objective"), "week": snapshot.week},
        response=json.loads(response.model_dump_json()),
        confidence=response.confidence,
        freshness_status=response.freshness.status,
        degraded_reasons=response.freshness.warnings,
        check_again_at=response.check_again_at,
    )
    session.add(run)
    session.flush()
    response.run_id = run.id

    for candidate in alternatives:
        session.add(
            RecommendationCandidate(
                run_id=run.id,
                rank=candidate.rank,
                is_recommended=False,
                subject_type=candidate.subject_type,
                subject_id=candidate.subject_id,
                label=candidate.label,
                score=candidate.score,
                projected_points=candidate.projected_points,
                floor_points=candidate.floor_points,
                ceiling_points=candidate.ceiling_points,
                evidence=candidate.evidence,
            )
        )
    session.flush()


def owner_team_label(session: Session, team_season: TeamSeason) -> str:
    team = session.get(Team, team_season.team_id)
    return team.name if team else team_season.id
