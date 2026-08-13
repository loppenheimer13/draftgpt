"""Canonical player identity reconciliation.

The single most dangerous silent failure in this system is a wrong player match:
every projection, roster, and recommendation downstream inherits it. So the
resolver is deliberately conservative -- it prefers to *refuse* and flag for
review over guessing.

Resolution order, strongest evidence first:
  1. Existing provider-id mapping (exact, already reconciled)
  2. Normalized full name + position + NFL team
  3. Normalized full name + position
  4. Known alias + position
  5. Last name + first initial + position + team (handles "A.J." vs "AJ")
Anything weaker is returned as ambiguous or unmatched, never auto-linked.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from draftgpt.database.models import Player, PlayerAlias, PlayerProviderId

SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}
_PUNCT = re.compile(r"[^\w\s]")
_WS = re.compile(r"\s+")

MatchMethod = Literal["provider_id", "exact", "alias", "initial", "fuzzy", "manual", "created"]


def normalize_name(name: str) -> str:
    """Casefold, strip accents/punctuation/suffixes, collapse whitespace.

    'A.J. Brown' -> 'aj brown'; 'Michael Pittman Jr.' -> 'michael pittman'.
    """
    if not name:
        return ""
    decomposed = unicodedata.normalize("NFKD", name)
    ascii_only = "".join(c for c in decomposed if not unicodedata.combining(c))
    cleaned = _PUNCT.sub("", ascii_only).casefold()
    tokens = [t for t in _WS.split(cleaned) if t]
    while tokens and tokens[-1] in SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def name_key(name: str) -> str:
    """Last name plus first initial. Deliberately lossy fallback key."""
    tokens = normalize_name(name).split()
    if not tokens:
        return ""
    if len(tokens) == 1:
        return tokens[0]
    return f"{tokens[0][0]}.{tokens[-1]}"


@dataclass(frozen=True)
class ResolutionResult:
    player_id: str | None
    method: MatchMethod | None
    confidence: float
    ambiguous_candidates: tuple[str, ...] = ()
    reason: str | None = None

    @property
    def resolved(self) -> bool:
        return self.player_id is not None

    @property
    def needs_review(self) -> bool:
        return self.confidence < 0.9 or bool(self.ambiguous_candidates)


class AmbiguousPlayerError(ValueError):
    """Raised when a user-supplied name matches more than one player.

    `/draft picked` must fail loudly rather than pick the wrong Josh Allen.
    """

    def __init__(self, query: str, candidates: list[tuple[str, str]]) -> None:
        listing = "; ".join(f"{name} ({pid[:8]})" for pid, name in candidates)
        super().__init__(f"'{query}' is ambiguous -- matches: {listing}")
        self.query = query
        self.candidates = candidates


class PlayerResolver:
    """Resolves provider payloads and human input to canonical player ids."""

    def __init__(self, session: Session) -> None:
        self.session = session

    # -- provider-driven resolution ---------------------------------------
    def resolve_provider_player(
        self,
        provider_key: str,
        external_id: str,
        name: str | None = None,
        position: str | None = None,
        nfl_team: str | None = None,
    ) -> ResolutionResult:
        existing = self.session.scalar(
            select(PlayerProviderId).where(
                PlayerProviderId.provider_key == provider_key,
                PlayerProviderId.external_id == str(external_id),
            )
        )
        if existing is not None:
            return ResolutionResult(existing.player_id, "provider_id", 1.0)

        if not name:
            return ResolutionResult(None, None, 0.0, reason="no name supplied to match on")

        normalized = normalize_name(name)
        candidates = list(
            self.session.scalars(
                select(Player).where(Player.normalized_name == normalized)
            )
        )

        if position:
            positional = [p for p in candidates if p.primary_position == position]
            if positional:
                candidates = positional

        if len(candidates) == 1:
            confidence = 0.98 if position else 0.9
            if nfl_team and candidates[0].nfl_team_id:
                confidence = min(1.0, confidence + 0.02)
            return ResolutionResult(candidates[0].id, "exact", confidence)

        if len(candidates) > 1:
            return ResolutionResult(
                None,
                None,
                0.4,
                ambiguous_candidates=tuple(p.id for p in candidates),
                reason=f"{len(candidates)} players share the normalized name '{normalized}'",
            )

        alias_match = self._resolve_alias(normalized, position)
        if alias_match is not None:
            return alias_match

        initial_match = self._resolve_by_initial(name, position)
        if initial_match is not None:
            return initial_match

        return ResolutionResult(None, None, 0.0, reason="no candidate found")

    def _resolve_alias(self, normalized: str, position: str | None) -> ResolutionResult | None:
        aliases = list(
            self.session.scalars(
                select(PlayerAlias).where(PlayerAlias.normalized_alias == normalized)
            )
        )
        if not aliases:
            return None
        player_ids = {a.player_id for a in aliases}
        if position:
            players = list(
                self.session.scalars(
                    select(Player).where(
                        Player.id.in_(player_ids), Player.primary_position == position
                    )
                )
            )
            player_ids = {p.id for p in players} or player_ids
        if len(player_ids) == 1:
            return ResolutionResult(next(iter(player_ids)), "alias", 0.93)
        return ResolutionResult(
            None, None, 0.4, ambiguous_candidates=tuple(sorted(player_ids)),
            reason="alias matches multiple players",
        )

    def _resolve_by_initial(self, name: str, position: str | None) -> ResolutionResult | None:
        key = name_key(name)
        if not key or "." not in key:
            return None
        query = select(Player)
        if position:
            query = query.where(Player.primary_position == position)
        matches = [p for p in self.session.scalars(query) if name_key(p.full_name) == key]
        if len(matches) == 1:
            return ResolutionResult(matches[0].id, "initial", 0.85)
        if len(matches) > 1:
            return ResolutionResult(
                None, None, 0.35, ambiguous_candidates=tuple(p.id for p in matches),
                reason="last-name/initial key matches multiple players",
            )
        return None

    # -- human-input resolution -------------------------------------------
    def resolve_query(self, query: str, position: str | None = None) -> Player:
        """Resolve a human-typed name to exactly one player, or raise.

        Used by `/draft picked` and `/roster compare`, where guessing wrong is
        materially worse than asking again.
        """
        normalized = normalize_name(query)
        if not normalized:
            raise ValueError("empty player query")

        stmt = select(Player).where(Player.normalized_name == normalized)
        if position:
            stmt = stmt.where(Player.primary_position == position)
        exact = list(self.session.scalars(stmt))
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            raise AmbiguousPlayerError(query, [(p.id, self._describe(p)) for p in exact])

        alias_ids = [
            a.player_id
            for a in self.session.scalars(
                select(PlayerAlias).where(PlayerAlias.normalized_alias == normalized)
            )
        ]
        if alias_ids:
            players = list(self.session.scalars(select(Player).where(Player.id.in_(alias_ids))))
            if len(players) == 1:
                return players[0]
            if len(players) > 1:
                raise AmbiguousPlayerError(query, [(p.id, self._describe(p)) for p in players])

        # Prefix / substring search, last resort and still ambiguity-checked.
        contains = [
            p
            for p in self.session.scalars(select(Player))
            if normalized and normalized in p.normalized_name
        ]
        if position:
            contains = [p for p in contains if p.primary_position == position] or contains
        if len(contains) == 1:
            return contains[0]
        if len(contains) > 1:
            raise AmbiguousPlayerError(query, [(p.id, self._describe(p)) for p in contains[:10]])

        raise LookupError(f"no player matches '{query}'")

    @staticmethod
    def _describe(player: Player) -> str:
        return f"{player.full_name} {player.primary_position}"

    # -- linking -----------------------------------------------------------
    def link(
        self,
        player_id: str,
        provider_key: str,
        external_id: str,
        external_name: str | None,
        method: MatchMethod,
        confidence: float,
    ) -> PlayerProviderId:
        existing = self.session.scalar(
            select(PlayerProviderId).where(
                PlayerProviderId.provider_key == provider_key,
                PlayerProviderId.external_id == str(external_id),
            )
        )
        if existing is not None:
            return existing
        mapping = PlayerProviderId(
            player_id=player_id,
            provider_key=provider_key,
            external_id=str(external_id),
            external_name=external_name,
            match_method=method if method != "provider_id" else "exact",
            match_confidence=confidence,
            needs_review=confidence < 0.9,
        )
        self.session.add(mapping)
        return mapping
