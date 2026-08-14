"""Derive strategy implications from league settings alone.

Everything here is deterministic and evidence-carrying: each finding names the
setting that produced it and the number that makes it true. No LLM, no
received fantasy wisdom -- if a claim cannot be computed from this league's
own rules, it does not belong in this module.

The point is that generic advice ("wait on QB", "RBs are scarce") is often
wrong for a specific league. In superflex the opposite is true of QBs; in a
3-WR/2-FLEX 14-teamer, WR demand outstrips RB. These rules read the actual
configuration instead of assuming a default.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from draftgpt.evaluation.lineup import SlotSpec
from draftgpt.evaluation.replacement import starters_by_position
from draftgpt.evaluation.scoring import ScoringRules

#: Severity ordering for presentation.
IMPACT_ORDER = {"defining": 0, "high": 1, "moderate": 2, "minor": 3}


@dataclass(frozen=True)
class Finding:
    """One strategy implication, with the evidence that produced it."""

    key: str
    headline: str
    detail: str
    impact: str = "moderate"  # defining | high | moderate | minor
    evidence: dict = field(default_factory=dict)


@dataclass
class LeagueShape:
    team_count: int
    starting_slots: list[SlotSpec]
    bench_slots: int
    ir_slots: int
    scoring: ScoringRules
    is_superflex: bool
    demand: dict[str, float]
    roster_size: int
    findings: list[Finding] = field(default_factory=list)

    @property
    def starters_per_team(self) -> int:
        return len(self.starting_slots)

    def demand_table(self) -> list[dict]:
        """League-wide starter demand per position, with replacement rank."""
        rows = []
        for position, count in sorted(self.demand.items(), key=lambda kv: -kv[1]):
            rows.append(
                {
                    "position": position,
                    "starters_league_wide": round(count, 1),
                    "replacement_rank": max(1, int(round(count))),
                    "per_team": round(count / self.team_count, 2),
                }
            )
        return rows


def analyze_league(
    scoring: ScoringRules,
    slots: list[SlotSpec],
    team_count: int,
    bench_slots: int = 0,
    ir_slots: int = 0,
    faab_budget: int | None = None,
    playoff_weeks: list[int] | None = None,
) -> LeagueShape:
    starting = [s for s in slots if s.eligible_positions]
    demand = starters_by_position(starting, team_count)
    is_superflex = any(
        "QB" in s.eligible_positions and len(s.eligible_positions) > 1 for s in starting
    )

    shape = LeagueShape(
        team_count=team_count,
        starting_slots=starting,
        bench_slots=bench_slots,
        ir_slots=ir_slots,
        scoring=scoring,
        is_superflex=is_superflex,
        demand=demand,
        roster_size=len(starting) + bench_slots,
    )

    shape.findings = [
        f
        for f in (
            _quarterback_finding(shape),
            _reception_finding(shape),
            _pass_td_finding(shape),
            _flex_finding(shape, starting),
            _tight_end_finding(shape),
            _scarcity_finding(shape),
            _bench_finding(shape),
            _bonus_finding(shape),
            _kicker_defense_finding(shape, starting),
            _faab_finding(shape, faab_budget),
            _playoff_finding(shape, playoff_weeks),
        )
        if f is not None
    ]
    shape.findings.sort(key=lambda f: IMPACT_ORDER.get(f.impact, 9))
    return shape


# --------------------------------------------------------------------------
def _quarterback_finding(shape: LeagueShape) -> Finding | None:
    qb_demand = shape.demand.get("QB", 0.0)
    if shape.is_superflex:
        return Finding(
            key="superflex_qb",
            headline="Superflex: quarterback is the scarcest asset in the league",
            detail=(
                f"About {qb_demand:.0f} QB starting spots exist across {shape.team_count} "
                "teams, against roughly 32 startable NFL starters. Demand approaches supply, "
                "so the replacement-level QB is far worse than the QB1 you would get by "
                "waiting. Securing two startable quarterbacks early is usually correct here, "
                "and is the single largest departure from standard-league advice."
            ),
            impact="defining",
            evidence={"qb_starting_spots": round(qb_demand, 1), "superflex": True},
        )
    if qb_demand <= shape.team_count * 1.05:
        return Finding(
            key="single_qb",
            headline="Single-QB league: quarterback is the most replaceable position",
            detail=(
                f"{qb_demand:.0f} QB spots across {shape.team_count} teams, against ~32 "
                "startable NFL starters. Roughly 20 usable quarterbacks go undrafted or late, "
                "so the gap between the QB1 and QB12 is small relative to the gap at RB or WR. "
                "Spending an early pick here forfeits scarcity elsewhere."
            ),
            impact="high",
            evidence={"qb_starting_spots": round(qb_demand, 1), "superflex": False},
        )
    return None


def _reception_finding(shape: LeagueShape) -> Finding | None:
    value = shape.scoring.reception_value
    if value >= 1.0:
        label, impact = "Full PPR", "high"
        detail = (
            f"Each reception is worth {value:g} points. A 70-catch season is {70 * value:.0f} "
            "points before a single yard, which lifts pass-catching running backs and "
            "high-volume slot receivers above what raw yardage suggests. Target share matters "
            "more than efficiency."
        )
    elif value > 0:
        label, impact = "Half PPR", "moderate"
        detail = (
            f"Each reception is worth {value:g} points -- {70 * value:.0f} points over a "
            "70-catch season. Volume still matters but the gap between a possession receiver "
            "and a big-play receiver narrows relative to full PPR."
        )
    else:
        label, impact = "Standard (no PPR)", "high"
        detail = (
            "Receptions score nothing. Yardage and touchdowns are everything, which pushes "
            "value toward high-volume rushers and away from short-area pass catchers. "
            "Most public rankings assume PPR and will systematically mislead you here."
        )
    return Finding(
        key="reception_scoring",
        headline=f"{label}: reception value is {value:g} points",
        detail=detail,
        impact=impact,
        evidence={"points_per_reception": value},
    )


def _pass_td_finding(shape: LeagueShape) -> Finding | None:
    value = shape.scoring.per_stat.get("pass_td")
    if value is None:
        return None
    if value >= 6:
        return Finding(
            key="pass_td_six",
            headline="6-point passing touchdowns raise quarterback value",
            detail=(
                "Most rankings assume 4-point passing TDs. At 6, a 35-TD quarterback gains "
                "70 points over that baseline, which narrows the gap between an elite QB and "
                "the flex players you would otherwise prioritise. Adjust public rankings upward "
                "for high-volume passers."
            ),
            impact="moderate",
            evidence={"points_per_passing_td": value},
        )
    return Finding(
        key="pass_td_four",
        headline=f"{value:g}-point passing touchdowns keep quarterback value contained",
        detail=(
            "This is the common default that public rankings assume, so no adjustment is "
            "needed. It keeps quarterbacks below elite skill players in raw scoring."
        ),
        impact="minor",
        evidence={"points_per_passing_td": value},
    )


def _flex_finding(shape: LeagueShape, starting: list[SlotSpec]) -> Finding | None:
    flex_slots = [
        s
        for s in starting
        if len(s.eligible_positions) > 1 and "QB" not in s.eligible_positions
    ]
    if not flex_slots:
        return None
    rb = shape.demand.get("RB", 0.0)
    wr = shape.demand.get("WR", 0.0)
    leader = "WR" if wr > rb else "RB"
    return Finding(
        key="flex_demand",
        headline=(
            f"{len(flex_slots)} flex slot(s) push league-wide demand to "
            f"{wr:.0f} WR and {rb:.0f} RB"
        ),
        detail=(
            f"Flex slots are absorbed mostly by {leader}s in practice. Across "
            f"{shape.team_count} teams that means roughly {max(wr, rb):.0f} startable "
            f"{leader}s are needed every week, so the replacement level at {leader} sits "
            "deeper than the raw starter count implies -- and the drop-off past it is what "
            "should drive your early picks."
        ),
        impact="high",
        evidence={
            "flex_slots_per_team": len(flex_slots),
            "wr_demand": round(wr, 1),
            "rb_demand": round(rb, 1),
        },
    )


def _tight_end_finding(shape: LeagueShape) -> Finding | None:
    te_demand = shape.demand.get("TE", 0.0)
    dedicated = te_demand <= shape.team_count * 1.15
    if not dedicated:
        return None
    return Finding(
        key="tight_end",
        headline="One tight end per team: a positional cliff, not a gradient",
        detail=(
            f"About {te_demand:.0f} TE spots across {shape.team_count} teams. Tight end "
            "historically has a very short group of reliable producers followed by a long "
            "flat tail, so the practical choice is to secure one of the few or take the "
            "cheapest acceptable option and spend elsewhere. The middle is the trap."
        ),
        impact="moderate",
        evidence={"te_starting_spots": round(te_demand, 1)},
    )


def _scarcity_finding(shape: LeagueShape) -> Finding | None:
    total_starters = shape.starters_per_team * shape.team_count
    return Finding(
        key="league_size",
        headline=(
            f"{shape.team_count} teams x {shape.starters_per_team} starters = "
            f"{total_starters} weekly starting spots"
        ),
        detail=(
            f"Roster size is {shape.roster_size} ({shape.starters_per_team} starting, "
            f"{shape.bench_slots} bench), so {shape.roster_size * shape.team_count} players "
            "are rostered in total. The deeper that number, the thinner the waiver wire and "
            "the more a drafted starter is worth relative to in-season replacement."
        ),
        impact="moderate",
        evidence={
            "team_count": shape.team_count,
            "starting_spots": total_starters,
            "rostered_total": shape.roster_size * shape.team_count,
        },
    )


def _bench_finding(shape: LeagueShape) -> Finding | None:
    if shape.bench_slots <= 0:
        return None
    impact = "high" if shape.bench_slots >= 7 else "moderate" if shape.bench_slots >= 5 else "minor"
    if shape.bench_slots <= 4:
        detail = (
            f"Only {shape.bench_slots} bench spots. There is almost no room for upside "
            "stashes, so every pick should be someone you would consider starting. Handcuffs "
            "and lottery tickets cost you a usable player."
        )
    else:
        detail = (
            f"{shape.bench_slots} bench spots leave room for "
            f"{max(0, shape.bench_slots - 2)} speculative holds after covering bye weeks. "
            "That supports drafting upside late rather than safe backups."
        )
    return Finding(
        key="bench_depth",
        headline=f"{shape.bench_slots} bench spots set how much speculation you can afford",
        detail=detail,
        impact=impact,
        evidence={"bench_slots": shape.bench_slots, "ir_slots": shape.ir_slots},
    )


def _bonus_finding(shape: LeagueShape) -> Finding | None:
    if not shape.scoring.bonuses:
        return None
    described = ", ".join(
        f"{b.points:g} pts at {b.threshold:g} {b.stat}" for b in shape.scoring.bonuses
    )
    return Finding(
        key="bonuses",
        headline=f"{len(shape.scoring.bonuses)} threshold bonus(es) reward boom games",
        detail=(
            f"{described}. Bonuses reward variance: a player who hits the threshold twice is "
            "worth more than one who is steadily just below it, even at equal totals. Public "
            "rankings ignore these entirely, so high-ceiling players are underpriced here."
        ),
        impact="moderate",
        evidence={
            "bonuses": [
                {"stat": b.stat, "threshold": b.threshold, "points": b.points}
                for b in shape.scoring.bonuses
            ]
        },
    )


def _kicker_defense_finding(shape: LeagueShape, starting: list[SlotSpec]) -> Finding | None:
    has_k = any(s.slot == "K" for s in starting)
    has_dst = any(s.slot == "DST" for s in starting)
    if not (has_k or has_dst):
        return None
    positions = " and ".join(
        p for p, present in (("kicker", has_k), ("defense", has_dst)) if present
    )
    return Finding(
        key="stream_positions",
        headline=f"Draft {positions} last -- these are streaming positions",
        detail=(
            "Week-to-week performance at these positions is close to unpredictable, and "
            "replacements are always available on waivers. Any pick spent here before the "
            "final rounds costs a player with real expected value."
        ),
        impact="minor",
        evidence={"kicker": has_k, "defense": has_dst},
    )


def _faab_finding(shape: LeagueShape, faab_budget: int | None) -> Finding | None:
    if not faab_budget:
        return None
    return Finding(
        key="faab",
        headline=f"FAAB budget of {faab_budget} is a season-long resource",
        detail=(
            f"With {faab_budget} to spend across the season, a single must-add breakout is "
            f"typically worth 20-35% of budget ({int(faab_budget * 0.2)}-"
            f"{int(faab_budget * 0.35)}). Budget spent in week 1 on marginal depth is the "
            "most common way managers lose the player who actually decides their season."
        ),
        impact="moderate",
        evidence={"faab_budget": faab_budget},
    )


def _playoff_finding(shape: LeagueShape, playoff_weeks: list[int] | None) -> Finding | None:
    if not playoff_weeks:
        return None
    return Finding(
        key="playoff_weeks",
        headline=f"Playoffs run weeks {min(playoff_weeks)}-{max(playoff_weeks)}",
        detail=(
            "Only these weeks decide the title. A player's schedule in this window matters "
            "more than his season average, and a bye inside it is irrelevant while a bye in "
            "week 5 is not. Weigh late-season matchups when two players are otherwise close."
        ),
        impact="moderate",
        evidence={"playoff_weeks": playoff_weeks},
    )
