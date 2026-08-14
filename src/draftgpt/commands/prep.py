"""`/prep league` and `/prep draft` -- pre-draft analysis.

These answer the questions you have *before* a roster exists:

* ``prep league`` -- what do my settings actually imply for strategy?
* ``prep draft`` -- what does the player pool look like priced under my rules?
* ``prep player`` -- what is this specific player worth to me, and when does he go?

All three follow the same discipline as every other command: deterministic
calculation first, structured evidence, explicit freshness, and nothing
asserted that cannot be traced to a setting or a projection.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from draftgpt.config import CALCULATION_VERSION
from draftgpt.database.models import (
    AdpSnapshot,
    Draft,
    DraftPick,
    LeagueRules,
    LeagueSeason,
    NflTeam,
    Player,
    Projection,
    RosterSlot,
)
from draftgpt.domain.contract import (
    ActionItem,
    Candidate,
    CommandResponse,
    MaterialInput,
)
from draftgpt.domain.freshness import (
    evaluate_freshness,
    infer_season_phase,
    next_check_time,
)
from draftgpt.evaluation.board import (
    BoardPlayer,
    DraftBoard,
    build_board,
    picks_until_next_turn,
    roster_construction_plan,
    scarcity_report,
    survival_probability,
)
from draftgpt.evaluation.league_shape import LeagueShape, analyze_league
from draftgpt.evaluation.lineup import SlotSpec
from draftgpt.evaluation.scoring import ScoringRules, score_stat_line
from draftgpt.ingestion.identity import PlayerResolver
from draftgpt.ingestion.runner import collect_freshness

logger = logging.getLogger(__name__)


@dataclass
class PrepContext:
    league_season: LeagueSeason
    rules: LeagueRules
    scoring: ScoringRules
    shape: LeagueShape
    slots: list[SlotSpec]


def load_context(session: Session, league_season_id: str | None = None) -> PrepContext:
    league_season = (
        session.get(LeagueSeason, league_season_id)
        if league_season_id
        else session.scalars(select(LeagueSeason)).first()
    )
    if league_season is None:
        raise LookupError("no league synced -- run `draftgpt sync league` first")

    rules = session.scalars(
        select(LeagueRules)
        .where(LeagueRules.league_season_id == league_season.id)
        .order_by(LeagueRules.version.desc())
    ).first()
    if rules is None:
        raise LookupError("league rules not synced")

    scoring = ScoringRules.from_config(rules.scoring, f"{rules.id}:v{rules.version}")
    all_slots = list(
        session.scalars(
            select(RosterSlot)
            .where(RosterSlot.league_rules_id == rules.id)
            .order_by(RosterSlot.display_order)
        )
    )
    starting = [
        SlotSpec(
            slot_id=row.id,
            slot=row.slot,
            ordinal=row.ordinal,
            eligible_positions=tuple(row.eligible_positions or ()),
            display_order=row.display_order,
        )
        for row in all_slots
        if row.is_starting
    ]
    bench = sum(1 for row in all_slots if not row.is_starting and row.slot == "BE")

    shape = analyze_league(
        scoring=scoring,
        slots=starting,
        team_count=league_season.team_count or 12,
        bench_slots=bench,
        ir_slots=rules.ir_slots,
        faab_budget=rules.faab_budget,
        playoff_weeks=league_season.playoff_weeks or [],
    )
    return PrepContext(league_season, rules, scoring, shape, starting)


# --------------------------------------------------------------------------
# prep league
# --------------------------------------------------------------------------
def prep_league(session: Session, league_season_id: str | None = None) -> CommandResponse:
    """Explain what this league's settings imply for strategy."""
    ctx = load_context(session, league_season_id)
    generated_at = datetime.now(UTC)
    shape = ctx.shape

    phase = infer_season_phase(generated_at, league_status=ctx.league_season.status)
    freshness, multiplier = evaluate_freshness(
        collect_freshness(session), "draft", str(phase), now=generated_at
    )

    findings = [
        {
            "key": f.key,
            "headline": f.headline,
            "detail": f.detail,
            "impact": f.impact,
            "evidence": f.evidence,
        }
        for f in shape.findings
    ]

    return CommandResponse(
        command="prep",
        subcommand="league",
        league_id=ctx.league_season.league_id,
        snapshot_id=f"rules:{ctx.rules.id}:v{ctx.rules.version}",
        generated_at=generated_at,
        calculation_version=CALCULATION_VERSION,
        recommendation={
            "league": {
                "season": ctx.league_season.season,
                "team_count": shape.team_count,
                "roster_size": shape.roster_size,
                "starters_per_team": shape.starters_per_team,
                "bench_slots": shape.bench_slots,
                "ir_slots": shape.ir_slots,
                "is_superflex": shape.is_superflex,
                "scoring_format": _scoring_label(ctx.scoring),
                "waiver_type": ctx.rules.waiver_type,
                "faab_budget": ctx.rules.faab_budget,
                "playoff_weeks": ctx.league_season.playoff_weeks,
            },
            "starting_lineup": [
                f"{s.slot}{s.ordinal + 1 if _slot_count(shape, s.slot) > 1 else ''}"
                for s in shape.starting_slots
            ],
            "positional_demand": shape.demand_table(),
            "findings": findings,
            "scoring_rules": _scoring_summary(ctx.scoring),
        },
        confidence=round(0.95 * multiplier, 3),
        reasons=[f.headline for f in shape.findings],
        material_inputs=[
            MaterialInput(
                key=f"setting:{f.key}",
                label=f.headline,
                value=f.evidence,
                effective_at=ctx.rules.effective_from,
            )
            for f in shape.findings
        ],
        freshness=freshness,
        do_now=[
            ActionItem(
                action="Review the defining and high-impact findings before building a board",
                urgency="normal",
                deeper_command="/prep draft",
            )
        ],
        check_again_at=next_check_time(str(phase), generated_at),
    )


def _slot_count(shape: LeagueShape, slot: str) -> int:
    return sum(1 for s in shape.starting_slots if s.slot == slot)


def _scoring_label(scoring: ScoringRules) -> str:
    value = scoring.reception_value
    if value >= 1.0:
        return "PPR"
    if value > 0:
        return f"{value:g} PPR"
    return "Standard"


def _scoring_summary(scoring: ScoringRules) -> dict:
    return {
        "per_stat": {k: v for k, v in sorted(scoring.per_stat.items()) if v},
        "bonuses": [
            {
                "stat": b.stat,
                "threshold": b.threshold,
                "points": b.points,
                "repeatable": b.repeatable,
            }
            for b in scoring.bonuses
        ],
        "points_allowed_tiers": [
            {"min": t.min_points, "max": t.max_points, "points": t.points}
            for t in scoring.points_allowed_tiers
        ],
    }


# --------------------------------------------------------------------------
# Board assembly
# --------------------------------------------------------------------------
def load_board(
    session: Session,
    ctx: PrepContext,
    scope: str = "season",
    as_of: datetime | None = None,
) -> DraftBoard:
    """Build the draft board from stored season projections and ADP."""
    as_of = as_of or datetime.now(UTC)
    season = ctx.league_season.season

    stmt = select(Projection).where(
        Projection.season == season,
        Projection.scope == scope,
        Projection.retrieved_at <= as_of,
    )
    latest: dict[str, Projection] = {}
    for projection in session.scalars(stmt.order_by(Projection.retrieved_at)):
        latest[projection.player_id] = projection

    if not latest:
        return DraftBoard(
            warnings=[
                f"no {scope} projections stored -- run "
                f"`draftgpt sync projections --scope {scope}`"
            ]
        )

    players = {
        p.id: p
        for p in session.scalars(select(Player).where(Player.id.in_(list(latest))))
    }
    teams = {t.id: t for t in session.scalars(select(NflTeam))}

    adp_rows: dict[str, AdpSnapshot] = {}
    for row in session.scalars(
        select(AdpSnapshot)
        .where(AdpSnapshot.season == season, AdpSnapshot.retrieved_at <= as_of)
        .order_by(AdpSnapshot.retrieved_at)
    ):
        adp_rows[row.player_id] = row

    drafted = _drafted_player_ids(session, ctx.league_season.id)

    board_players: list[BoardPlayer] = []
    for player_id, projection in latest.items():
        player = players.get(player_id)
        if player is None:
            continue
        points = (
            score_stat_line(projection.stat_line, ctx.scoring)
            if projection.stat_line
            else (projection.projected_points or 0.0)
        )
        if points <= 0:
            continue
        team = teams.get(player.nfl_team_id) if player.nfl_team_id else None
        adp = adp_rows.get(player_id)
        board_players.append(
            BoardPlayer(
                player_id=player_id,
                name=player.full_name,
                position=player.primary_position,
                nfl_team=team.abbreviation if team else None,
                projected_points=points,
                floor_points=projection.floor_points,
                ceiling_points=projection.ceiling_points,
                adp=adp.adp if adp else None,
                adp_stdev=adp.adp_stdev if adp else None,
                bye_week=_bye_week(team, season),
                status=player.status,
                is_drafted=player_id in drafted,
            )
        )

    return build_board(board_players, ctx.shape)


def _bye_week(team: NflTeam | None, season: int) -> int | None:
    if team is None:
        return None
    raw = (team.bye_week_by_season or {}).get(str(season))
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _drafted_player_ids(session: Session, league_season_id: str) -> set[str]:
    draft = session.scalar(select(Draft).where(Draft.league_season_id == league_season_id))
    if draft is None:
        return set()
    return {
        pick.player_id
        for pick in session.scalars(
            select(DraftPick).where(
                DraftPick.draft_id == draft.id, DraftPick.is_active.is_(True)
            )
        )
    }


# --------------------------------------------------------------------------
# prep draft
# --------------------------------------------------------------------------
def prep_draft(
    session: Session,
    league_season_id: str | None = None,
    position: str | None = None,
    limit: int = 40,
    draft_position: int | None = None,
    at_pick: int | None = None,
    tier_max: int | None = None,
    as_of: datetime | None = None,
) -> CommandResponse:
    """The draft board, priced under this league's rules.

    Options narrow the same underlying board rather than computing a different
    one, so a positional view and the overall view can never disagree.
    """
    ctx = load_context(session, league_season_id)
    generated_at = datetime.now(UTC)
    board = load_board(session, ctx, as_of=as_of)

    phase = infer_season_phase(generated_at, league_status=ctx.league_season.status)
    freshness, multiplier = evaluate_freshness(
        collect_freshness(session), "draft", str(phase), now=generated_at
    )

    entries = board.available()
    if position:
        entries = [e for e in entries if e.player.position == position.upper()]
    if tier_max is not None:
        entries = [e for e in entries if e.tier <= tier_max]
    shown = entries[:limit]

    # Availability at a specific pick, when the owner tells us where they sit.
    availability: list[dict] = []
    if at_pick is not None:
        for entry in shown:
            probability = survival_probability(
                entry.player.adp, entry.player.adp_stdev, at_pick
            )
            if probability is not None:
                availability.append(
                    {
                        "player": entry.player.name,
                        "player_id": entry.player.player_id,
                        "adp": entry.player.adp,
                        "survival_probability": probability,
                    }
                )

    gap_picks = (
        picks_until_next_turn(
            at_pick, draft_position, ctx.shape.team_count
        )
        if at_pick and draft_position
        else None
    )

    reasons = _board_reasons(board, ctx.shape, shown, position)
    warnings = list(board.warnings)

    return CommandResponse(
        command="prep",
        subcommand=f"draft{f' {position.upper()}' if position else ''}",
        league_id=ctx.league_season.league_id,
        snapshot_id=f"board:{ctx.rules.id}:{len(board.entries)}",
        generated_at=generated_at,
        calculation_version=CALCULATION_VERSION,
        recommendation={
            "board": [e.to_dict() for e in shown],
            "total_ranked": len(board.entries),
            "filtered_to": len(entries),
            "scarcity": scarcity_report(board, ctx.shape),
            "roster_plan": roster_construction_plan(ctx.shape, ctx.shape.roster_size),
            "tier_breaks": board.tier_breaks,
            "availability_at_pick": availability or None,
            "picks_until_next_turn": gap_picks,
            "warnings": warnings,
        },
        alternatives=[
            Candidate(
                rank=e.overall_rank,
                label=e.player.name,
                subject_id=e.player.player_id,
                projected_points=round(e.player.projected_points, 1),
                value_over_replacement=e.value_over_replacement,
                tier=e.tier,
                evidence=e.to_dict(),
            )
            for e in shown[:5]
        ],
        confidence=round((0.8 if board.entries else 0.2) * multiplier, 3),
        reasons=reasons,
        material_inputs=[
            MaterialInput(
                key=f"board:{e.player.player_id}",
                label=e.player.name,
                value=e.to_dict(),
            )
            for e in shown
        ],
        freshness=freshness,
        do_now=[
            ActionItem(
                action="Study the tier cliffs, not the ordering",
                detail=(
                    "The ranking tells you who is better; the cliffs tell you when waiting "
                    "costs you a tier."
                ),
                urgency="normal",
            )
        ],
        watch=[ActionItem(action=w, urgency="normal") for w in warnings],
        check_again_at=next_check_time(str(phase), generated_at),
    )


def _board_reasons(
    board: DraftBoard, shape: LeagueShape, shown: list, position: str | None
) -> list[str]:
    if not board.entries:
        return ["No projections are loaded, so no board can be built."]

    reasons = [
        f"Ranked {len(board.entries)} players by value over replacement under this "
        f"league's scoring, not by raw projected points."
    ]
    for pos, level in sorted(board.replacement.items()):
        reasons.append(
            f"{pos} replacement level is {level.points:.1f} points "
            f"({pos}{level.baseline_rank}), derived from {shape.demand.get(pos, 0):.0f} "
            f"starting spots across {shape.team_count} teams."
        )
    cliffs = [e for e in shown if e.is_last_of_tier]
    for entry in cliffs[:5]:
        reasons.append(
            f"{entry.player.name} is the last of {entry.player.position} tier {entry.tier} -- "
            f"the next one down is {entry.points_to_next_tier:.1f} points worse."
        )
    if position:
        reasons.append(f"Filtered to {position.upper()}.")
    return reasons


# --------------------------------------------------------------------------
# prep player
# --------------------------------------------------------------------------
def prep_player(
    session: Session,
    query: str,
    league_season_id: str | None = None,
    at_pick: int | None = None,
    as_of: datetime | None = None,
) -> CommandResponse:
    """Deep dive on one player, priced for this league.

    Name resolution refuses ambiguity rather than guessing, so asking about
    "Mike Williams" returns an error naming both candidates.
    """
    ctx = load_context(session, league_season_id)
    generated_at = datetime.now(UTC)
    player = PlayerResolver(session).resolve_query(query)

    board = load_board(session, ctx, as_of=as_of)
    entry = board.find(player.id)

    phase = infer_season_phase(generated_at, league_status=ctx.league_season.status)
    freshness, multiplier = evaluate_freshness(
        collect_freshness(session), "draft", str(phase), now=generated_at
    )

    if entry is None:
        return CommandResponse(
            command="prep",
            subcommand="player",
            league_id=ctx.league_season.league_id,
            snapshot_id=f"board:{ctx.rules.id}",
            generated_at=generated_at,
            calculation_version=CALCULATION_VERSION,
            recommendation={
                "player": player.full_name,
                "position": player.primary_position,
                "note": "no projection stored for this player, so no value can be computed",
            },
            confidence=0.0,
            reasons=[
                f"{player.full_name} resolved to a canonical player but has no "
                f"{ctx.league_season.season} projection loaded."
            ],
            freshness=freshness,
            check_again_at=next_check_time(str(phase), generated_at),
        )

    same_position = [e for e in board.by_position(entry.player.position)]
    index = same_position.index(entry)
    nearby = same_position[max(0, index - 2) : index + 3]

    probability = (
        survival_probability(entry.player.adp, entry.player.adp_stdev, at_pick)
        if at_pick
        else None
    )

    reasons = [
        f"{entry.player.name} projects {entry.player.projected_points:.1f} points under "
        f"this league's scoring, {entry.value_over_replacement:+.1f} versus the "
        f"{entry.player.position} replacement level.",
        f"He is {entry.player.position}{entry.position_rank}, tier {entry.tier}, "
        f"overall value rank {entry.overall_rank}.",
    ]
    if entry.is_last_of_tier and entry.points_to_next_tier is not None:
        reasons.append(
            f"He is the last of his tier -- the next {entry.player.position} down is "
            f"{entry.points_to_next_tier:.1f} points worse, so passing here means "
            "dropping a tier."
        )
    if entry.adp_delta is not None:
        if entry.adp_delta > 8:
            reasons.append(
                f"ADP {entry.player.adp:.0f} against value rank {entry.overall_rank}: the "
                "market lets him fall further than his value warrants."
            )
        elif entry.adp_delta < -8:
            reasons.append(
                f"ADP {entry.player.adp:.0f} against value rank {entry.overall_rank}: the "
                "market drafts him earlier than value justifies."
            )
    if probability is not None:
        reasons.append(
            f"Roughly {probability:.0%} chance he is still there at pick {at_pick} "
            "(ADP dispersion is estimated, so treat this as a guide)."
        )
    if entry.player.bye_week:
        reasons.append(f"Bye week {entry.player.bye_week}.")

    return CommandResponse(
        command="prep",
        subcommand="player",
        league_id=ctx.league_season.league_id,
        snapshot_id=f"board:{ctx.rules.id}",
        generated_at=generated_at,
        calculation_version=CALCULATION_VERSION,
        recommendation={
            **entry.to_dict(),
            "survival_probability_at_pick": probability,
            "at_pick": at_pick,
            "positional_neighbours": [e.to_dict() for e in nearby],
        },
        alternatives=[
            Candidate(
                rank=e.overall_rank,
                label=e.player.name,
                subject_id=e.player.player_id,
                projected_points=round(e.player.projected_points, 1),
                value_over_replacement=e.value_over_replacement,
                tier=e.tier,
                evidence=e.to_dict(),
            )
            for e in nearby
            if e.player.player_id != entry.player.player_id
        ],
        confidence=round(0.8 * multiplier, 3),
        reasons=reasons,
        material_inputs=[
            MaterialInput(key=f"board:{entry.player.player_id}", label=entry.player.name,
                          value=entry.to_dict())
        ]
        + [
            MaterialInput(key=f"board:{e.player.player_id}", label=e.player.name,
                          value=e.to_dict())
            for e in nearby
        ],
        freshness=freshness,
        check_again_at=next_check_time(str(phase), generated_at),
    )
