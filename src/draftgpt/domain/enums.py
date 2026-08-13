"""Canonical vocabularies. Provider values are normalized into these."""

from __future__ import annotations

from enum import StrEnum


class Position(StrEnum):
    QB = "QB"
    RB = "RB"
    WR = "WR"
    TE = "TE"
    K = "K"
    DST = "DST"
    DL = "DL"
    LB = "LB"
    DB = "DB"


class SlotType(StrEnum):
    QB = "QB"
    RB = "RB"
    WR = "WR"
    TE = "TE"
    FLEX = "FLEX"          # RB/WR/TE
    WR_RB_FLEX = "WRRB"    # RB/WR
    WR_TE_FLEX = "WRTE"    # WR/TE
    SUPERFLEX = "SFLEX"    # QB/RB/WR/TE
    OP = "OP"              # offensive player, same as superflex on most platforms
    K = "K"
    DST = "DST"
    DP = "DP"              # defensive player
    BENCH = "BE"
    IR = "IR"


#: Which canonical positions may legally fill each slot.
SLOT_ELIGIBILITY: dict[str, tuple[str, ...]] = {
    SlotType.QB: ("QB",),
    SlotType.RB: ("RB",),
    SlotType.WR: ("WR",),
    SlotType.TE: ("TE",),
    SlotType.FLEX: ("RB", "WR", "TE"),
    SlotType.WR_RB_FLEX: ("RB", "WR"),
    SlotType.WR_TE_FLEX: ("WR", "TE"),
    SlotType.SUPERFLEX: ("QB", "RB", "WR", "TE"),
    SlotType.OP: ("QB", "RB", "WR", "TE"),
    SlotType.K: ("K",),
    SlotType.DST: ("DST",),
    SlotType.DP: ("DL", "LB", "DB"),
    SlotType.BENCH: (),
    SlotType.IR: (),
}

STARTING_SLOTS: frozenset[str] = frozenset(
    s for s in SLOT_ELIGIBILITY if s not in (SlotType.BENCH, SlotType.IR)
)


class FreshnessClass(StrEnum):
    """Section 4. Determines how stale an input may be before it degrades a
    decision, and how hard a command tries to refresh it."""

    STATIC = "static"   # league rules, historical results
    SLOW = "slow"       # preseason projections, tiers, ADP
    DAILY = "daily"     # weekly projections, depth charts, usage
    RAPID = "rapid"     # practice reports, injuries, transactions, lines, weather
    LIVE = "live"       # draft picks, inactives, game-day status


class SeasonPhase(StrEnum):
    EARLY_PRESEASON = "early_preseason"
    PRE_DRAFT = "pre_draft"
    DRAFT_DAY = "draft_day"
    REGULAR_SEASON = "regular_season"
    GAME_DAY = "game_day"
    OFFSEASON = "offseason"


class Capability(StrEnum):
    """What an adapter can serve. Registered per provider; commands ask the
    registry for a capability rather than naming a provider."""

    LEAGUE_SETTINGS = "league_settings"
    LEAGUE_ROSTERS = "league_rosters"
    LEAGUE_MATCHUPS = "league_matchups"
    LEAGUE_TRANSACTIONS = "league_transactions"
    LEAGUE_DRAFT = "league_draft"
    LEAGUE_FREE_AGENTS = "league_free_agents"
    LEAGUE_WAIVER_STATE = "league_waiver_state"
    PLAYER_REGISTRY = "player_registry"
    PROJECTIONS_WEEKLY = "projections_weekly"
    PROJECTIONS_SEASON = "projections_season"
    PROJECTIONS_ROS = "projections_ros"
    RANKINGS = "rankings"
    TIERS = "tiers"
    ADP = "adp"
    MARKET_TRENDS = "market_trends"
    SCHEDULE = "schedule"
    INJURIES = "injuries"
    DEPTH_CHARTS = "depth_charts"
    USAGE = "usage"
    WEATHER = "weather"
    BETTING_CONTEXT = "betting_context"
    NEWS = "news"


#: Canonical stat keys. Provider stat codes normalize into these before scoring,
#: so one scoring engine serves every projection source.
STAT_KEYS: tuple[str, ...] = (
    # Passing
    "pass_att", "pass_cmp", "pass_inc", "pass_yd", "pass_td", "pass_int", "pass_2pt",
    "pass_sack",
    # Rushing
    "rush_att", "rush_yd", "rush_td", "rush_2pt",
    # Receiving
    "rec_tgt", "rec", "rec_yd", "rec_td", "rec_2pt",
    # Misc offense
    "fum", "fum_lost", "fum_rec_td", "return_td", "two_pt_conv",
    # Kicking
    "fg_made", "fg_miss", "fg_made_0_39", "fg_made_40_49", "fg_made_50_plus",
    "xp_made", "xp_miss",
    # Team defense
    "def_sack", "def_int", "def_fum_rec", "def_td", "def_safety", "def_block",
    "def_pts_allowed", "def_yds_allowed",
    # IDP
    "idp_tackle_solo", "idp_tackle_ast", "idp_sack", "idp_int", "idp_ff", "idp_fr", "idp_td",
)
