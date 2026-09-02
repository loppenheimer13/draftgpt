"""The kid-safety filter.

This is the part of Huddle that most justifies its existence. Sports news is
written for adults: the same feed that carries a walk-off home run carries
arrests, betting lines, contract disputes, and injuries described in clinical
detail. A children's show cannot pipe that through and hope.

So the filter is **deny by default**. A story reaches the microphone only if it
matches an allowed shape *and* trips nothing on the block list. Anything the
filter is unsure about is dropped -- there is always another story, and the
cost of a miss is a child hearing something a parent did not agree to.

Four gates, in order. A story must clear every one:

1. **Type** -- the provider's own story classification.
2. **Block list** -- phrase matching on headline, summary, and categories.
3. **Shape** -- the story must *positively* match something a children's show
   airs: a game result, a milestone, a debut, an explainer. This gate is what
   makes the filter sound rather than merely long. A block list alone always
   loses, because sports feeds invent new adult phrasings faster than anyone
   maintains a word list -- during development this filter's block list passed
   a drunk-driving plea and a contract stalemate simply because nobody had
   thought to add those words yet. Requiring a recognised safe shape turns
   every unanticipated story into a silent drop instead of a broadcast.
4. **Editorial** -- an optional model pass, enabled with narration, which can
   only ever *remove* a story, never admit one the earlier gates rejected.

The word lists and patterns live in ``content/safety.yaml`` so they can be
edited without touching code; the defaults below are what ship.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from huddle.domain.enums import SafetyVerdict

logger = logging.getLogger(__name__)

CONTENT_DIR = Path(__file__).resolve().parents[1] / "content"
SAFETY_FILE = CONTENT_DIR / "safety.yaml"

#: Story shapes worth airing. Anything else -- opinion columns, insider
#: rumours, betting explainers -- is dropped without inspection.
DEFAULT_ALLOWED_TYPES: tuple[str, ...] = (
    "recap", "preview", "story", "news", "headlinenews", "headline", "media",
    "highlights", "feature", "article",
)

#: Topics that never belong in a children's show, as literal phrases. Matching
#: is on word boundaries so "draft" does not catch "draught" and "bet" does not
#: catch "better".
DEFAULT_BLOCKED_TERMS: dict[str, tuple[str, ...]] = {
    "betting": (
        "bet", "bets", "betting", "odds", "spread", "moneyline", "parlay",
        "sportsbook", "wager", "wagering", "gambling", "gamble", "over/under",
        "point spread", "prop bet", "favorite to win at", "book it",
    ),
    "legal": (
        "arrest", "arrested", "charged with", "lawsuit", "sued", "indicted",
        "police", "court", "trial", "guilty", "felony", "assault", "domestic",
        "investigation", "allegation", "alleged", "misconduct", "scandal",
        "banned", "suspended for violating", "appeal hearing",
        "pleads", "pleaded", "plea", "no contest", "owi", "citation",
        "custody", "sentenced", "probation",
    ),
    "substances": (
        "doping", "steroid", "steroids", "peds", "performance-enhancing",
        "substance abuse", "drug test", "positive test", "alcohol",
        "dui", "marijuana", "cannabis", "vaping",
    ),
    "graphic_injury": (
        "torn acl", "ruptured", "surgery", "carted off", "stretcher",
        "concussion", "blood", "gruesome", "fracture", "fractured", "dislocated",
        "season-ending injury", "hospitalized", "ambulance", "collapsed",
    ),
    "death_and_harm": (
        "died", "death", "dead", "killed", "fatal", "shooting", "shot",
        "suicide", "mental health crisis", "overdose", "funeral", "memorial",
        "tragedy", "crash",
    ),
    "adult": (
        "divorce", "affair", "girlfriend drama", "feud", "trash talk",
        "profanity", "expletive", "obscene", "vulgar", "nightclub",
    ),
    "money_talk": (
        "contract dispute", "holdout", "salary cap", "million-dollar deal",
        "trade demand", "fired", "sacked", "ousted", "buyout", "grievance",
        "blackball", "blackballing", "collusion", "without deal", "franchise tag",
    ),
    # Adult sports-talk noise. Fantasy analysis is the loudest category in every
    # US sports feed and is meaningless to a child, so it is blocked outright
    # rather than merely deprioritized.
    "fantasy_and_analysis": (
        "fantasy football", "fantasy basketball", "fantasy baseball", "fantasy hockey",
        "fantasy value", "fantasy buzz", "start/sit", "start or sit", "waiver wire",
        "mock draft", "draft rankings", "sleepers", "busts", "adp", "dfs",
        "daily fantasy", "trade value", "power rankings", "roster percentage",
        "players to draft", "lineup advice", "must-start", "streamers",
        "fantasy manager", "fantasy managers", "d/st", "flex play", "waiver",
        "matchup ratings", "cheat sheet", "auction values", "keeper league",
    ),
    # Injury *lists* are routine sports news but land badly in a kids' show,
    # and "injured reserve" phrasing needs adult context to parse.
    "injury_status": (
        "injured reserve", "placed on ir", "out for the season", "season-ending",
        "designated to return", "questionable to return", "day-to-day",
        "injury report", "recovery", "rehab", "sidelined", "setback",
        "miss remainder", "miss the rest", "out indefinitely", "will miss",
        "ruled out", "limped", "exited the game", "left the game with",
        # Parenthetical body parts are the standard wire notation for an
        # injury designation and are a reliable signal on their own.
        "(knee)", "(ankle)", "(hamstring)", "(shoulder)", "(foot)", "(back)",
        "(elbow)", "(wrist)", "(groin)", "(calf)", "(quad)", "(illness)",
        "(concussion)", "(hip)", "(oblique)", "(thumb)", "(finger)",
        "injury", "injuries", "injured",
    ),
    # Transfer markets, contracts, and club ownership. Real sports news, and
    # entirely adult -- a seven-year-old has no use for a record fee.
    "business": (
        "transfer window", "transfer deadline", "transfer fee", "record fee",
        "signings", "signs for", "loan deal", "release clause", "bid for",
        "ownership group", "limited partner", "investor", "valuation", "stake",
        "record deal", "inking", "inks", "contract extension", "worth up to",
        "trade deadline", "free agency", "agent said",
    ),
    # Tabloid sourcing. If a story cannot name who said it, a kids' show has
    # no business repeating it.
    "rumour": (
        "sources", "reportedly", "rumor", "rumors", "rumour", "rumours",
        "insider", "u-turn", "fume", "fumes", "slams", "blasts", "hits back",
        "speculation", "linked with", "eyeing a move", "unhappy",
    ),
}

#: Shapes a children's sportscast airs. A story must match one of these to
#: run at all. Ordered loosely by how common they are in a daily feed.
DEFAULT_ALLOW_PATTERNS: tuple[tuple[str, str], ...] = (
    # A result, with or without a scoreline: "Reds beat Padres 4-3".
    (
        "result",
        r"\b(beat|beats|defeat|defeats|top|tops|edge|edges|rally|rallies|"
        r"outlast|outlasts|sweep|sweeps|shut ?out|hold off|holds off|"
        r"win|wins|won|victory|comeback|advance|advances|clinch|clinches)\b",
    ),
    # Highlight reels and "where to watch" -- always safe, always useful.
    ("highlights", r"\b(game highlights|highlights|where to watch|schedule|preview)\b"),
    # Achievements and firsts, the natural heart of a kids' sports show.
    (
        "milestone",
        r"\b(record|records|milestone|career-high|first .{0,30}\bto\b|"
        r"no-hitter|perfect game|hat ?trick|triple-double|double-double|"
        r"walk-off|grand slam|shutout|hall of fame|"
        r"retire[sd]?\b[^.]{0,40}(?:no\.?\s*\d+|number\s*\d+|jersey)|"
        r"jersey|debut|debuts|rookie|all-star|champion|championship|title)\b",
    ),
    # Explainers: exactly the "one thing to know" the show promises.
    ("explainer", r"\b(why|what|how|explained|means|guide to|inside)\b.{0,60}\?"),
)

#: Phrases that make an otherwise-fine story unsuitable *as a lead*, usually
#: because they need adult framing to make sense.
DEFAULT_CAUTION_TERMS: tuple[str, ...] = (
    "retirement", "retires", "traded", "waived", "released", "benched",
    "protest", "boycott", "controversy", "criticized", "ejected", "penalty",
)


@dataclass
class SafetyPolicy:
    allowed_types: tuple[str, ...] = DEFAULT_ALLOWED_TYPES
    blocked_terms: dict[str, tuple[str, ...]] = field(
        default_factory=lambda: dict(DEFAULT_BLOCKED_TERMS)
    )
    caution_terms: tuple[str, ...] = DEFAULT_CAUTION_TERMS
    allow_patterns: tuple[tuple[str, str], ...] = DEFAULT_ALLOW_PATTERNS
    #: Stories shorter than this have no substance to explain to a child.
    min_summary_chars: int = 40
    #: Require a positive shape match. Turning this off reverts to block-list
    #: behaviour and is not recommended -- see the module docstring.
    require_shape_match: bool = True


@dataclass(frozen=True)
class SafetyResult:
    verdict: str
    #: Why it was blocked, for the audit log the parent dashboard shows.
    category: str | None = None
    matched: str | None = None
    #: True when the story is airable but should not lead the segment.
    caution: bool = False
    #: Which safe shape admitted it, when it was admitted on shape.
    shape: str | None = None

    @property
    def allowed(self) -> bool:
        return self.verdict == SafetyVerdict.ALLOW


def _build_patterns(policy: SafetyPolicy) -> dict[str, re.Pattern[str]]:
    """One alternation per category, so a hit names the category it tripped."""
    compiled: dict[str, re.Pattern[str]] = {}
    for category, terms in policy.blocked_terms.items():
        if not terms:
            continue
        compiled[category] = re.compile(
            r"(?<!\w)(?:" + "|".join(re.escape(t) for t in terms) + r")(?!\w)",
            re.IGNORECASE,
        )
    if policy.caution_terms:
        compiled["__caution__"] = re.compile(
            r"(?<!\w)(?:" + "|".join(re.escape(t) for t in policy.caution_terms) + r")(?!\w)",
            re.IGNORECASE,
        )
    return compiled


class SafetyFilter:
    """Applies a policy to candidate stories and to finished show copy."""

    def __init__(self, policy: SafetyPolicy | None = None) -> None:
        self.policy = policy or load_policy()
        self._patterns = _build_patterns(self.policy)
        self._shapes = [
            (name, re.compile(pattern, re.IGNORECASE))
            for name, pattern in self.policy.allow_patterns
        ]

    def match_shape(self, text: str) -> str | None:
        """Name the safe shape this text matches, if any."""
        for name, pattern in self._shapes:
            if pattern.search(text or ""):
                return name
        return None

    def check_text(self, text: str) -> SafetyResult:
        """Scan free text. Used on headlines, summaries, and written scripts.

        Running this over the *finished* script as well as the source stories
        is deliberate: it is the backstop that catches a blocked phrase the
        narration model reintroduced while rephrasing.
        """
        haystack = text or ""
        for category, pattern in self._patterns.items():
            if category == "__caution__":
                continue
            match = pattern.search(haystack)
            if match:
                return SafetyResult(
                    verdict=SafetyVerdict.BLOCK, category=category, matched=match.group(0)
                )

        caution_pattern = self._patterns.get("__caution__")
        caution = bool(caution_pattern and caution_pattern.search(haystack))
        return SafetyResult(verdict=SafetyVerdict.ALLOW, caution=caution)

    def check_story(self, story: dict[str, Any]) -> SafetyResult:
        """Judge one candidate news story.

        ``story`` is the normalized shape the provider adapters emit:
        ``{type, headline, summary, categories}``.
        """
        story_type = (story.get("type") or "").strip().lower()
        if story_type and story_type not in self.policy.allowed_types:
            return SafetyResult(
                verdict=SafetyVerdict.BLOCK, category="type", matched=story_type
            )

        headline = story.get("headline") or ""
        summary = story.get("summary") or ""
        if len(f"{headline} {summary}".strip()) < self.policy.min_summary_chars:
            return SafetyResult(
                verdict=SafetyVerdict.BLOCK, category="too_thin", matched="no summary"
            )

        # Categories are provider-supplied labels; scanning them catches a
        # story whose headline is bland but whose subject is not.
        blob = " ".join(
            [headline, summary, " ".join(str(c) for c in story.get("categories") or ())]
        )
        blocked = self.check_text(blob)
        if not blocked.allowed:
            return blocked

        if not self.policy.require_shape_match:
            return blocked

        # Gate 3. A recap is a game result by definition, so it needs no
        # further shape evidence; everything else must look like something a
        # children's show would actually air.
        shape = "recap" if story_type == "recap" else self.match_shape(f"{headline} {summary}")
        if shape is None:
            return SafetyResult(
                verdict=SafetyVerdict.BLOCK, category="unrecognized_shape",
                matched=headline[:80],
            )
        return SafetyResult(
            verdict=SafetyVerdict.ALLOW, caution=blocked.caution, shape=shape
        )

    def filter_stories(self, stories: list[dict[str, Any]]) -> tuple[list[dict], list[dict]]:
        """Split candidates into (allowed, blocked-with-reason).

        Allowed stories keep a ``caution`` flag so the writer can order the
        gentle ones first.
        """
        allowed: list[dict[str, Any]] = []
        blocked: list[dict[str, Any]] = []
        for story in stories:
            result = self.check_story(story)
            if result.allowed:
                allowed.append({**story, "caution": result.caution, "shape": result.shape})
            else:
                blocked.append(
                    {
                        "headline": story.get("headline"),
                        "category": result.category,
                        "matched": result.matched,
                    }
                )
        if blocked:
            logger.info("safety filter dropped %s of %s stories", len(blocked), len(stories))
        return allowed, blocked


def load_policy(path: Path | None = None) -> SafetyPolicy:
    """Load the editable policy, falling back to the shipped defaults.

    A malformed file is a loud warning and a fallback, never a crash: losing
    the custom word list is bad, publishing an unfiltered show is worse.
    """
    path = path or SAFETY_FILE
    if not path.exists():
        return SafetyPolicy()

    try:
        import yaml

        data = yaml.safe_load(path.read_text()) or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not read %s (%s); using built-in safety defaults", path, exc)
        return SafetyPolicy()

    blocked = dict(DEFAULT_BLOCKED_TERMS)
    for category, terms in (data.get("blocked_terms") or {}).items():
        # Custom lists add to the defaults rather than replacing them, so an
        # incomplete edit cannot quietly disable a whole category.
        blocked[category] = tuple({*blocked.get(category, ()), *(terms or ())})

    extra_shapes = tuple(
        (entry["name"], entry["pattern"])
        for entry in (data.get("allow_patterns") or ())
        if isinstance(entry, dict) and entry.get("name") and entry.get("pattern")
    )
    return SafetyPolicy(
        allowed_types=tuple(data.get("allowed_types") or DEFAULT_ALLOWED_TYPES),
        blocked_terms=blocked,
        caution_terms=tuple({*DEFAULT_CAUTION_TERMS, *(data.get("caution_terms") or ())}),
        allow_patterns=DEFAULT_ALLOW_PATTERNS + extra_shapes,
        min_summary_chars=int(data.get("min_summary_chars") or 40),
        require_shape_match=bool(data.get("require_shape_match", True)),
    )
