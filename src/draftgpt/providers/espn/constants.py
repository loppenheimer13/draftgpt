"""ESPN Fantasy Football identifier maps.

ESPN publishes no schema for these identifiers. The maps below are the
community-consensus values. Treat them as **unverified until checked against
your own league**: `draftgpt sources verify-espn` dumps the league's raw scoring
items beside our normalization so any mismatch surfaces before it silently
corrupts every projection downstream.

Design rule: an unrecognized statId is never dropped. It is preserved in
``raw_settings['unmapped_scoring_items']`` and reported as a warning, because a
silently ignored scoring rule is the worst failure mode this module has.
"""

from __future__ import annotations

from draftgpt.domain.enums import SlotType

#: ESPN defaultPositionId -> canonical position.
POSITION_BY_ID: dict[int, str] = {
    1: "QB",
    2: "RB",
    3: "WR",
    4: "TE",
    5: "K",
    16: "DST",
    # Individual defensive players
    9: "DL",
    10: "LB",
    11: "DL",
    12: "DB",
    13: "DB",
    14: "DB",
}

#: ESPN lineupSlotId -> canonical slot.
SLOT_BY_ID: dict[int, str] = {
    0: SlotType.QB,
    2: SlotType.RB,
    3: SlotType.WR_RB_FLEX,
    4: SlotType.WR,
    5: SlotType.WR_TE_FLEX,
    6: SlotType.TE,
    7: SlotType.OP,
    16: SlotType.DST,
    17: SlotType.K,
    20: SlotType.BENCH,
    21: SlotType.IR,
    23: SlotType.FLEX,
}

#: Slot ids ESPN uses for individual defensive players.
IDP_SLOT_IDS: frozenset[int] = frozenset({8, 9, 10, 11, 12, 13, 14, 15, 24})

#: ESPN proTeamId -> team abbreviation. 0 is the free-agent / no-team sentinel.
PRO_TEAM_BY_ID: dict[int, str] = {
    0: "FA", 1: "ATL", 2: "BUF", 3: "CHI", 4: "CIN", 5: "CLE", 6: "DAL", 7: "DEN",
    8: "DET", 9: "GB", 10: "TEN", 11: "IND", 12: "KC", 13: "LV", 14: "LAR", 15: "MIA",
    16: "MIN", 17: "NE", 18: "NO", 19: "NYG", 20: "NYJ", 21: "PHI", 22: "ARI",
    23: "PIT", 24: "LAC", 25: "SF", 26: "SEA", 27: "TB", 28: "WSH", 29: "CAR",
    30: "JAX", 33: "BAL", 34: "HOU",
}

#: ESPN scoring statId -> canonical stat key.
#:
#: Confidence: offensive IDs (0-72) are well established and consistent across
#: every public client. Kicking (74-88) and defensive (89-129) IDs are less
#: consistently documented -- verify these against your league before trusting.
STAT_BY_ID: dict[int, str] = {
    # --- Passing (high confidence) ---
    0: "pass_att",
    1: "pass_cmp",
    2: "pass_inc",
    3: "pass_yd",
    4: "pass_td",
    19: "pass_2pt",
    20: "pass_int",
    21: "pass_sack",
    # --- Rushing (high confidence) ---
    23: "rush_att",
    24: "rush_yd",
    25: "rush_td",
    26: "rush_2pt",
    # --- Receiving (high confidence) ---
    41: "rec_tgt",
    42: "rec_yd",
    43: "rec_td",
    44: "rec_2pt",
    53: "rec",
    58: "rec_tgt",
    # --- Fumbles (high confidence) ---
    68: "fum",
    72: "fum_lost",
    # --- Kicking (medium confidence: verify) ---
    74: "fg_made_50_plus",
    77: "fg_made_40_49",
    80: "fg_made_0_39",
    83: "fg_made",
    85: "fg_miss",
    86: "xp_made",
    88: "xp_miss",
    # --- Team defense, non-bracketed (medium confidence: verify) ---
    93: "def_block",
    95: "def_int",
    96: "def_fum_rec",
    97: "def_block",
    98: "def_safety",
    99: "def_sack",
    101: "return_td",
    102: "return_td",
    103: "def_td",
    104: "def_td",
}

#: ESPN encodes team-defense points-allowed as one scoring item per bracket
#: rather than a tier table. statId -> (min_points, max_points) inclusive.
#: Medium confidence: verify against your league.
DEF_POINTS_ALLOWED_BRACKETS: dict[int, tuple[float, float]] = {
    89: (0, 0),
    90: (1, 6),
    91: (7, 13),
    92: (14, 17),
    123: (28, 34),
    124: (35, 45),
    125: (46, 99),
}

#: statId -> (min_yards, max_yards) inclusive. Medium confidence: verify.
DEF_YARDS_ALLOWED_BRACKETS: dict[int, tuple[float, float]] = {
    127: (0, 99),
    128: (100, 199),
    129: (200, 299),
    130: (300, 349),
    131: (350, 399),
    132: (400, 449),
    133: (450, 499),
    134: (500, 549),
    135: (550, 9999),
}

#: Display-only aggregates that carry no scoring meaning.
IGNORED_STAT_IDS: frozenset[int] = frozenset({210, 211, 212, 213, 214})

#: Canonical stat keys we are confident enough in to use without verification.
HIGH_CONFIDENCE_STAT_IDS: frozenset[int] = frozenset(
    {0, 1, 2, 3, 4, 19, 20, 21, 23, 24, 25, 26, 41, 42, 43, 44, 53, 58, 68, 72}
)

#: ESPN transaction type -> canonical transaction type.
TRANSACTION_TYPE: dict[str, str] = {
    "WAIVER": "waiver",
    "FREEAGENT": "free_agent",
    "TRADE_ACCEPT": "trade",
    "TRADE_PROPOSAL": "trade_proposal",
    "ROSTER": "roster_move",
    "DRAFT": "draft",
}

#: ESPN injuryStatus -> canonical designation.
INJURY_STATUS: dict[str, str] = {
    "ACTIVE": "ACTIVE",
    "NORMAL": "ACTIVE",
    "PROBABLE": "P",
    "QUESTIONABLE": "Q",
    "DAY_TO_DAY": "Q",
    "DOUBTFUL": "D",
    "OUT": "O",
    "INJURY_RESERVE": "IR",
    "SUSPENSION": "SUSP",
}

#: ESPN league status -> canonical league_seasons.status.
def league_status(is_active: bool, current_matchup_period: int, draft_complete: bool) -> str:
    if not draft_complete:
        return "drafting" if is_active else "pre_draft"
    if not is_active:
        return "complete"
    return "in_season" if current_matchup_period >= 1 else "pre_draft"
