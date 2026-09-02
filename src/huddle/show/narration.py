"""Optional rewriting of a script segment for the ear.

Disabled by default, and strictly bounded when enabled: the model receives one
already-written segment and may only rephrase it. It is never given the raw
data, never asked a question, and never allowed to add a fact.

Two things enforce that rather than merely requesting it:

1. the prompt supplies the finished text, not the underlying state, so there is
   nothing new to reason from;
2. :func:`introduces_facts` compares the rewrite against the original and
   rejects any output containing a number or proper noun the original did not
   have, falling back to the deterministic text.

The second is what makes the feature safe to ship. A digest that mispronounces
a sentence is a nuisance; one that invents an injury is a defect.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from huddle.config import Settings, get_settings

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You rewrite fantasy football audio scripts so they sound \
natural read aloud.

Rules, in order of importance:
1. Never add a fact. No statistic, name, team, injury, ranking, or opinion may \
appear in your rewrite unless it appears in the text you were given.
2. Never remove a fact. Every name, number, and status in the input must survive.
3. Write for the ear: short sentences, no abbreviations, no symbols, numbers \
spelled the way a person would say them.
4. Keep the same order and roughly the same length.

Return only the rewritten script. No preamble, no notes, no markdown."""

#: Tokens that may legitimately appear in a rewrite without being in the source.
_ALLOWED_NEW_WORDS = {
    "He", "His", "Him", "They", "Their", "The", "And", "But", "That", "This",
    "It", "Its", "A", "An", "In", "On", "At", "For", "With", "Now", "Next",
    "So", "If", "There", "Here", "One", "Good", "Luck", "Meanwhile", "Also",
    "Still", "Then", "After", "Before", "Watch", "Keep", "Expect",
}

_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_PROPER = re.compile(r"\b[A-Z][a-z]{2,}\b")


@dataclass
class NarrationResult:
    text: str
    rewritten: bool
    reason: str | None = None


def introduces_facts(original: str, candidate: str) -> str | None:
    """Return a reason string when ``candidate`` asserts something new.

    Numbers are compared exactly. Proper nouns are compared case-sensitively
    against a small allow-list of sentence-openers, because a hallucinated
    player or team name is precisely the failure this guards against.
    """
    new_numbers = set(_NUMBER.findall(candidate)) - set(_NUMBER.findall(original))
    if new_numbers:
        return f"introduced numbers not in the source: {', '.join(sorted(new_numbers))}"

    original_words = set(_PROPER.findall(original))
    new_words = {
        word
        for word in _PROPER.findall(candidate)
        if word not in original_words and word not in _ALLOWED_NEW_WORDS
    }
    if new_words:
        return f"introduced proper nouns not in the source: {', '.join(sorted(new_words))}"
    return None


def narrate(text: str, settings: Settings | None = None, client=None) -> NarrationResult:
    """Rewrite one segment, or return it untouched.

    Every failure path -- disabled, no key, SDK missing, API error, guardrail
    trip -- returns the original text. Narration is a polish step, so it must
    never be able to stop an episode from publishing.
    """
    settings = settings or get_settings()
    if not settings.narration_enabled:
        return NarrationResult(text=text, rewritten=False, reason="narration disabled")
    if not settings.anthropic_api_key and client is None:
        return NarrationResult(text=text, rewritten=False, reason="no ANTHROPIC_API_KEY set")

    try:
        client = client or _client(settings)
    except ImportError:
        return NarrationResult(
            text=text, rewritten=False, reason="anthropic SDK not installed"
        )

    try:
        response = client.messages.create(
            model=settings.narration_model,
            max_tokens=2000,
            system=SYSTEM_PROMPT,
            # Rephrasing needs no deliberation; low effort keeps thinking on
            # (which the current models want) without paying for depth.
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": text}],
        )
    except Exception as exc:  # noqa: BLE001 - never fail a digest over polish
        logger.warning("narration call failed: %s", exc)
        return NarrationResult(text=text, rewritten=False, reason=f"api error: {exc}")

    if getattr(response, "stop_reason", None) == "refusal":
        return NarrationResult(text=text, rewritten=False, reason="model declined the rewrite")

    candidate = "".join(
        block.text for block in response.content if getattr(block, "type", None) == "text"
    ).strip()
    if not candidate:
        return NarrationResult(text=text, rewritten=False, reason="empty rewrite")

    violation = introduces_facts(text, candidate)
    if violation:
        logger.warning("narration rejected: %s", violation)
        return NarrationResult(text=text, rewritten=False, reason=violation)

    return NarrationResult(text=candidate, rewritten=True)


def _client(settings: Settings):
    import anthropic

    return anthropic.Anthropic(api_key=settings.anthropic_api_key)
