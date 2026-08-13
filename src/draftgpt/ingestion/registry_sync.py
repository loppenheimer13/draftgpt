"""Canonical player registry sync.

Builds the player table from nflverse and populates ``player_provider_ids`` with
every cross-platform identifier nflverse carries. After this runs, an ESPN
roster entry resolves by *identifier* rather than by name, which removes the
single largest silent-failure risk in the system.

Name resolution remains the fallback for the ~16% of fantasy-relevant players
nflverse has no ``espn_id`` for (measured on 2024 rosters) -- typically rookies
and in-season callups. That path is load bearing, so it stays conservative:
ambiguity raises rather than guesses.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from draftgpt.database.models import (
    NflTeam,
    Player,
    PlayerAlias,
    PlayerProviderId,
    PlayerSeasonEligibility,
)
from draftgpt.ingestion.identity import PlayerResolver, normalize_name
from draftgpt.ingestion.runner import fetch_with_run, upsert_provider
from draftgpt.providers.base import Provider
from draftgpt.providers.registry import build

logger = logging.getLogger(__name__)


@dataclass
class RegistrySyncReport:
    players_created: int = 0
    players_updated: int = 0
    provider_ids_linked: int = 0
    aliases_created: int = 0
    espn_id_coverage: float = 0.0
    unresolved: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.players_created} created, {self.players_updated} updated, "
            f"{self.provider_ids_linked} provider ids linked, "
            f"espn coverage {self.espn_id_coverage:.1%}"
        )


def ensure_nfl_teams(session: Session) -> dict[str, NflTeam]:
    """Idempotently create NFL team rows from the ESPN abbreviation map."""
    from draftgpt.providers.espn.constants import PRO_TEAM_BY_ID

    existing = {t.abbreviation: t for t in session.scalars(select(NflTeam))}
    for abbreviation in PRO_TEAM_BY_ID.values():
        if abbreviation in existing or abbreviation == "FA":
            continue
        team = NflTeam(abbreviation=abbreviation, name=abbreviation)
        session.add(team)
        existing[abbreviation] = team
    session.flush()
    return existing


def sync_player_registry(
    session: Session,
    season: int,
    provider: Provider | None = None,
    fantasy_only: bool = True,
) -> RegistrySyncReport:
    """Load nflverse players and link every provider identifier they carry."""
    provider = provider or build("nflverse")
    upsert_provider(session, provider.spec)
    teams = ensure_nfl_teams(session)
    report = RegistrySyncReport()

    result = fetch_with_run(
        session,
        provider,
        "player_registry",
        scope=f"season:{season}",
        season=season,
        fantasy_only=fantasy_only,
    )
    if result is None:
        report.notes.append("player registry fetch failed; registry unchanged")
        return report
    report.notes.extend(result.notes)

    # Index existing state once rather than querying per row.
    by_gsis = {
        m.external_id: m.player_id
        for m in session.scalars(
            select(PlayerProviderId).where(PlayerProviderId.provider_key == "nflverse")
        )
    }
    existing_links = {
        (m.provider_key, m.external_id)
        for m in session.scalars(select(PlayerProviderId))
    }
    espn_seen = 0

    for record in result.records:
        gsis_id = record["gsis_id"]
        name = (record.get("name") or "").strip()
        if not name:
            continue

        player_id = by_gsis.get(gsis_id)
        if player_id is None:
            player = Player(
                full_name=name,
                normalized_name=normalize_name(name),
                first_name=record.get("first_name"),
                last_name=record.get("last_name"),
                primary_position=record.get("position") or "UNK",
                nfl_team_id=_team_id(teams, record.get("team")),
                jersey_number=record.get("jersey_number"),
                status=_canonical_status(record.get("status")),
                is_team_defense=record.get("position") == "DST",
                provider_id=None,
                retrieved_at=result.retrieved_at or datetime.now(UTC),
            )
            session.add(player)
            session.flush()
            player_id = player.id
            by_gsis[gsis_id] = player_id
            report.players_created += 1
        else:
            player = session.get(Player, player_id)
            if player is not None:
                player.nfl_team_id = _team_id(teams, record.get("team")) or player.nfl_team_id
                player.status = _canonical_status(record.get("status"))
                if record.get("position"):
                    player.primary_position = record["position"]
                report.players_updated += 1

        # Season-effective eligibility, preserved rather than overwritten.
        _record_eligibility(session, player_id, season, record, teams)

        for provider_key, external_id in (record.get("provider_ids") or {}).items():
            if provider_key == "espn":
                espn_seen += 1
            if (provider_key, str(external_id)) in existing_links:
                continue
            session.add(
                PlayerProviderId(
                    player_id=player_id,
                    provider_key=provider_key,
                    external_id=str(external_id),
                    external_name=name,
                    match_method="exact",
                    match_confidence=1.0,
                    needs_review=False,
                )
            )
            existing_links.add((provider_key, str(external_id)))
            report.provider_ids_linked += 1

        report.aliases_created += _ensure_aliases(session, player_id, record)

    total = len(result.records) or 1
    report.espn_id_coverage = espn_seen / total
    session.flush()
    logger.info("player registry sync: %s", report.summary())
    return report


def _record_eligibility(
    session: Session,
    player_id: str,
    season: int,
    record: dict,
    teams: dict[str, NflTeam],
) -> None:
    positions = [record["position"]] if record.get("position") else []
    existing = session.scalar(
        select(PlayerSeasonEligibility).where(
            PlayerSeasonEligibility.player_id == player_id,
            PlayerSeasonEligibility.season == season,
        )
    )
    if existing is not None:
        if existing.positions == positions:
            return
        # Position changed mid-season (rare but real). Keep the old row.
        existing = None
    session.add(
        PlayerSeasonEligibility(
            player_id=player_id,
            season=season,
            nfl_team_id=_team_id(teams, record.get("team")),
            positions=positions,
            effective_at=datetime.now(UTC),
        )
    )


def _ensure_aliases(session: Session, player_id: str, record: dict) -> int:
    """Store a 'first last' alias when the full name differs from its parts.

    Catches the 'Marquise Brown' vs 'Hollywood Brown' class of mismatch between
    a league platform's display name and nflverse's roster name.
    """
    first, last = record.get("first_name"), record.get("last_name")
    if not (first and last):
        return 0
    candidate = f"{first} {last}"
    normalized = normalize_name(candidate)
    if not normalized or normalized == normalize_name(record.get("name") or ""):
        return 0
    exists = session.scalar(
        select(PlayerAlias).where(
            PlayerAlias.player_id == player_id,
            PlayerAlias.normalized_alias == normalized,
        )
    )
    if exists is not None:
        return 0
    session.add(
        PlayerAlias(
            player_id=player_id,
            alias=candidate,
            normalized_alias=normalized,
            source="nflverse",
        )
    )
    return 1


def sync_players_from_league_provider(
    session: Session,
    provider: Provider,
    season: int,
    create_missing: bool = True,
) -> RegistrySyncReport:
    """Reconcile a league platform's player pool into the canonical registry.

    nflverse is the preferred identity source, but it does not cover everyone:
    ``espn_id`` is absent for roughly 16% of fantasy-relevant players (measured
    on 2024 rosters), and rookies and in-season callups appear on a platform
    before nflverse publishes them. Without this pass those players would be
    silently missing from every lineup decision.

    Players created here are marked with the league provider as their origin,
    so a later nflverse sync can enrich rather than duplicate them.
    """
    upsert_provider(session, provider.spec)
    teams = ensure_nfl_teams(session)
    report = RegistrySyncReport()
    resolver = PlayerResolver(session)

    result = fetch_with_run(
        session, provider, "player_registry", scope=f"season:{season}", required=False
    )
    if result is None:
        report.notes.append(f"{provider.spec.key} player registry unavailable")
        return report

    provider_key = provider.spec.key
    for record in result.records:
        external_id = str(record.get("player_external_id") or record.get("external_id") or "")
        name = (record.get("name") or "").strip()
        if not external_id or not name:
            continue

        outcome = resolver.resolve_provider_player(
            provider_key=provider_key,
            external_id=external_id,
            name=name,
            position=record.get("position"),
            nfl_team=record.get("pro_team") or record.get("team"),
        )

        if outcome.resolved:
            if outcome.method != "provider_id":
                resolver.link(
                    player_id=outcome.player_id,  # type: ignore[arg-type]
                    provider_key=provider_key,
                    external_id=external_id,
                    external_name=name,
                    method=outcome.method or "fuzzy",
                    confidence=outcome.confidence,
                )
                report.provider_ids_linked += 1
            report.players_updated += 1
            continue

        if outcome.ambiguous_candidates:
            # Never guess between real candidates -- flag for manual review.
            report.unresolved.append(f"{name} (ambiguous: {outcome.reason})")
            continue
        if not create_missing:
            report.unresolved.append(f"{name} ({outcome.reason or 'not found'})")
            continue

        player = Player(
            full_name=name,
            normalized_name=normalize_name(name),
            first_name=record.get("first_name"),
            last_name=record.get("last_name"),
            primary_position=record.get("position") or "UNK",
            nfl_team_id=_team_id(teams, record.get("pro_team") or record.get("team")),
            status="active",
            is_team_defense=bool(record.get("is_team_defense")),
            retrieved_at=result.retrieved_at or datetime.now(UTC),
        )
        session.add(player)
        session.flush()
        resolver.link(
            player_id=player.id,
            provider_key=provider_key,
            external_id=external_id,
            external_name=name,
            method="manual",
            confidence=1.0 if provider_key.startswith("fixture") else 0.85,
        )
        report.players_created += 1
        report.provider_ids_linked += 1

    session.flush()
    logger.info("league-provider registry sync (%s): %s", provider_key, report.summary())
    return report


def link_league_players(
    session: Session,
    provider_key: str,
    entries: list[dict],
) -> tuple[int, list[str]]:
    """Link a league platform's player entries to canonical players.

    Returns the number linked and the list of names that could not be resolved.
    Unresolved players are reported, never silently dropped -- an unlinked
    starter would otherwise vanish from lineup optimization.
    """
    resolver = PlayerResolver(session)
    linked = 0
    unresolved: list[str] = []

    for entry in entries:
        external_id = entry.get("player_external_id")
        if not external_id:
            continue
        outcome = resolver.resolve_provider_player(
            provider_key=provider_key,
            external_id=str(external_id),
            name=entry.get("name"),
            position=entry.get("position"),
            nfl_team=entry.get("pro_team"),
        )
        if not outcome.resolved:
            label = entry.get("name") or str(external_id)
            reason = outcome.reason or "unresolved"
            unresolved.append(f"{label} ({reason})")
            continue
        if outcome.method != "provider_id":
            resolver.link(
                player_id=outcome.player_id,  # type: ignore[arg-type]
                provider_key=provider_key,
                external_id=str(external_id),
                external_name=entry.get("name"),
                method=outcome.method or "fuzzy",
                confidence=outcome.confidence,
            )
        linked += 1

    session.flush()
    return linked, unresolved


def _team_id(teams: dict[str, NflTeam], abbreviation: str | None) -> str | None:
    if not abbreviation or abbreviation == "FA":
        return None
    team = teams.get(abbreviation)
    return team.id if team else None


def _canonical_status(status: str | None) -> str:
    if not status:
        return "active"
    upper = status.upper()
    if upper.startswith("ACT"):
        return "active"
    if "RES" in upper or upper == "IR":
        return "ir"
    if "SUS" in upper:
        return "suspended"
    if "CUT" in upper or "RET" in upper:
        return "retired"
    return "active"
