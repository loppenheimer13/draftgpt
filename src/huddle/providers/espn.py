"""ESPN public JSON adapter -- one adapter for every league in the catalogue.

The same five endpoint shapes serve the NFL, the WNBA, LaLiga, and men's
college basketball, which is what makes a fourteen-league show tractable:

    /{sport}/{league}/teams            club list for the parent's picker
    /{sport}/{league}/teams/{id}       record, standing, next or last game
    /{sport}/{league}/teams/{id}/roster athlete birthdays for Birthday Club
    /{sport}/{league}/scoreboard       today's scores and schedule
    /{sport}/{league}/news             candidate stories, before safety filtering

Posture
-------
These are the publicly readable JSON endpoints ESPN's own web client calls.
Huddle reads them, at low volume, once a day per followed league, and stores
only the facts it narrates -- scores, times, names, birthdays. It does not
scrape HTML, does not redistribute payloads, and writes nothing back. Article
text is never republished: a headline is a *candidate*, and what reaches the
audio is rewritten from scratch.

The adapter is read-only by construction. There is one ``_get`` method and no
post, put, patch, or delete anywhere in this module.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from typing import Any

import httpx

from huddle.catalog import LeagueSpec, get_league
from huddle.config import Settings
from huddle.domain.enums import Capability, FreshnessClass, GameState
from huddle.providers.base import (
    BaseProvider,
    FetchResult,
    ProviderSpec,
    ProviderUnavailable,
)
from huddle.providers.registry import register

logger = logging.getLogger(__name__)

PROVIDER_KEY = "espn"
API_BASE = "https://site.api.espn.com/apis/site/v2/sports"

ATTRIBUTION = "Scores, schedules and team data via ESPN's public endpoints."

SPEC = ProviderSpec(
    key=PROVIDER_KEY,
    display_name="ESPN (public JSON)",
    categories=("teams", "scores", "news", "athletes"),
    capabilities=(
        Capability.LEAGUE_TEAMS,
        Capability.TEAM_DETAIL,
        Capability.TEAM_ROSTER,
        Capability.SCOREBOARD,
        Capability.LEAGUE_NEWS,
    ),
    freshness_class=FreshnessClass.RAPID,
    requires_auth=False,
    rate_limit_per_min=60,
    raw_retention_days=7,
    license_note=(
        "Publicly readable JSON endpoints used by ESPN's own web client, read "
        "at low volume. No HTML scraping, no payload redistribution, no writes. "
        "Article text is never republished -- headlines are candidates only, "
        "and the broadcast copy is written from scratch."
    ),
    fallback="use the last successful snapshot and say so on air",
)

#: ESPN status states map onto ours one-for-one.
_STATE_MAP = {"pre": GameState.SCHEDULED, "in": GameState.IN_PROGRESS, "post": GameState.FINAL}


class EspnProvider(BaseProvider):
    spec = SPEC

    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        self._settings = settings
        self._owned = client is None
        self._http = client or httpx.Client(
            timeout=settings.http_timeout_seconds,
            headers={"Accept": "application/json"},
            follow_redirects=True,
        )

    def close(self) -> None:
        if self._owned:
            self._http.close()

    def health_check(self) -> tuple[bool, str]:
        try:
            self._get("football/nfl", "teams", params={"limit": 1})
        except ProviderUnavailable as exc:
            return False, str(exc)
        return True, "ok"

    # -- transport ---------------------------------------------------------
    def _get(self, league_path: str, endpoint: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{API_BASE}/{league_path}/{endpoint}"
        try:
            response = self._http.get(url, params=params)
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(PROVIDER_KEY, endpoint, f"{url} unreachable: {exc}") from exc
        if response.status_code >= 400:
            raise ProviderUnavailable(
                PROVIDER_KEY, endpoint, f"{url} returned {response.status_code}"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise ProviderUnavailable(PROVIDER_KEY, endpoint, f"{url} returned non-JSON") from exc

    def _result(self, records: list[dict[str, Any]], **kw: Any) -> FetchResult:
        notes = list(kw.pop("notes", []))
        notes.append(ATTRIBUTION)
        return FetchResult(
            capability="", records=records, retrieved_at=datetime.now(UTC), notes=notes, **kw
        )

    # -- teams -------------------------------------------------------------
    def fetch_league_teams(self, league: str, **_: Any) -> FetchResult:
        spec = get_league(league)
        payload = self._get(spec.provider_path, "teams", params={"limit": 1000})
        records = [
            record
            for group in payload.get("sports", [])
            for sub in group.get("leagues", [])
            for entry in sub.get("teams", [])
            if (record := _team_record(entry.get("team") or {}, spec))
        ]
        return self._result(records, notes=[f"{len(records)} teams in {spec.display_name}"])

    def fetch_team_detail(self, league: str, team_external_id: str, **_: Any) -> FetchResult:
        """Record, standing, and the team's most relevant single game.

        ESPN's ``nextEvent`` is the team's *next or most recent* event -- it
        carries a final score once a game ends -- so one call covers both "how
        did they do" and "when are they on next".
        """
        spec = get_league(league)
        payload = self._get(spec.provider_path, f"teams/{team_external_id}")
        team = payload.get("team") or {}
        record = _record_summary(team.get("record"))

        games = [
            game
            for event in (team.get("nextEvent") or [])
            if (game := _game_record(event, spec))
        ]
        return self._result(
            [
                {
                    **(_team_record(team, spec) or {}),
                    "record_summary": record,
                    "standing_summary": team.get("standingSummary"),
                    "games": games,
                }
            ]
        )

    def fetch_team_roster(self, league: str, team_external_id: str, **_: Any) -> FetchResult:
        spec = get_league(league)
        payload = self._get(spec.provider_path, f"teams/{team_external_id}/roster")
        records = [
            athlete
            for entry in _flatten_athletes(payload.get("athletes") or [])
            if (athlete := _athlete_record(entry, spec, team_external_id))
        ]
        return self._result(records)

    # -- scoreboard --------------------------------------------------------
    def fetch_scoreboard(self, league: str, on: date | None = None, **_: Any) -> FetchResult:
        spec = get_league(league)
        params = {"dates": on.strftime("%Y%m%d")} if on else None
        payload = self._get(spec.provider_path, "scoreboard", params=params)
        records = [
            game for event in payload.get("events", []) if (game := _game_record(event, spec))
        ]
        notes = []
        if not records and not spec.in_season(_month(on)):
            notes.append(f"{spec.display_name} is out of season")
        return self._result(records, notes=notes)

    # -- news --------------------------------------------------------------
    def fetch_league_news(self, league: str, limit: int = 20, **_: Any) -> FetchResult:
        """Candidate stories. Nothing here is airable until the safety filter
        has ruled on it -- this adapter deliberately makes no such judgement."""
        spec = get_league(league)
        payload = self._get(spec.provider_path, "news", params={"limit": limit})
        records = [
            story
            for article in payload.get("articles", [])
            if (story := _news_record(article, spec))
        ]
        return self._result(records, notes=["unfiltered candidates; safety filter runs downstream"])


# -- normalizers -----------------------------------------------------------
def _team_record(team: dict[str, Any], spec: LeagueSpec) -> dict[str, Any] | None:
    external_id = team.get("id")
    display = team.get("displayName")
    if not external_id or not display:
        return None
    location = team.get("location")
    short = team.get("shortDisplayName") or team.get("name") or display
    logos = team.get("logos") or []
    return {
        "external_id": str(external_id),
        "league_key": spec.key,
        "display_name": display,
        "short_name": short,
        "location": location,
        "abbreviation": team.get("abbreviation"),
        "search_name": " ".join(
            part for part in (location, team.get("name"), display, team.get("abbreviation")) if part
        ).lower(),
        "slug": team.get("slug"),
        "color": team.get("color"),
        "logo_url": (logos[0] or {}).get("href") if logos else None,
        "active": bool(team.get("isActive", True)),
    }


def _game_record(event: dict[str, Any], spec: LeagueSpec) -> dict[str, Any] | None:
    competitions = event.get("competitions") or []
    if not competitions:
        return None
    competition = competitions[0]
    status = (competition.get("status") or event.get("status") or {}).get("type") or {}

    home = away = None
    for competitor in competition.get("competitors") or []:
        side = competitor.get("homeAway")
        if side == "home":
            home = competitor
        elif side == "away":
            away = competitor

    notes = [n.get("headline") for n in (competition.get("notes") or []) if n.get("headline")]
    return {
        "external_id": str(event.get("id")),
        "league_key": spec.key,
        "name": event.get("name"),
        "start_at": _parse_datetime(event.get("date") or competition.get("date")),
        "state": str(_STATE_MAP.get(str(status.get("state") or ""), GameState.SCHEDULED)),
        "status_detail": status.get("detail") or status.get("description"),
        "completed": bool(status.get("completed")),
        "home_team_external_id": _competitor_team_id(home),
        "away_team_external_id": _competitor_team_id(away),
        "home_team_name": _competitor_team_name(home),
        "away_team_name": _competitor_team_name(away),
        "home_score": _score(home),
        "away_score": _score(away),
        "venue": (competition.get("venue") or {}).get("fullName"),
        "broadcast": _broadcast(competition),
        "extra": {
            "neutral_site": bool(competition.get("neutralSite")),
            "notes": notes,
            "week": (event.get("week") or {}).get("number"),
            "season_type": (event.get("season") or {}).get("type"),
        },
    }


def _competitor_team_id(competitor: dict[str, Any] | None) -> str | None:
    if not competitor:
        return None
    team = competitor.get("team") or {}
    return str(team.get("id")) if team.get("id") else None


def _competitor_team_name(competitor: dict[str, Any] | None) -> str | None:
    if not competitor:
        return None
    team = competitor.get("team") or {}
    return team.get("displayName") or team.get("name")


def _score(competitor: dict[str, Any] | None) -> int | None:
    """ESPN returns a score as a bare string in some sports and an object in
    others; a pre-game zero is not a score and must stay ``None``."""
    if not competitor:
        return None
    raw = competitor.get("score")
    if isinstance(raw, dict):
        raw = raw.get("value", raw.get("displayValue"))
    if raw in (None, ""):
        return None
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


def _broadcast(competition: dict[str, Any]) -> str | None:
    for entry in competition.get("broadcasts") or []:
        names = entry.get("names") or []
        if names:
            return str(names[0])
        media = (entry.get("media") or {}).get("shortName")
        if media:
            return str(media)
    return None


def _record_summary(record: Any) -> str | None:
    for item in (record or {}).get("items") or []:
        if item.get("type") == "total" or item.get("description") == "Overall Record":
            return item.get("summary")
    return None


def _flatten_athletes(athletes: list[Any]) -> list[dict[str, Any]]:
    """Rosters arrive either flat or grouped by position depending on sport."""
    flat: list[dict[str, Any]] = []
    for entry in athletes:
        if isinstance(entry, dict) and "items" in entry:
            flat.extend(i for i in entry["items"] if isinstance(i, dict))
        elif isinstance(entry, dict):
            flat.append(entry)
    return flat


def _athlete_record(
    athlete: dict[str, Any], spec: LeagueSpec, team_external_id: str
) -> dict[str, Any] | None:
    external_id = athlete.get("id")
    name = athlete.get("fullName") or athlete.get("displayName")
    if not external_id or not name:
        return None
    birth = _parse_date(athlete.get("dateOfBirth"))
    place = athlete.get("birthPlace") or {}
    return {
        "external_id": str(external_id),
        "league_key": spec.key,
        "team_external_id": str(team_external_id),
        "full_name": name,
        "position": (athlete.get("position") or {}).get("displayName"),
        "jersey": athlete.get("jersey"),
        "birth_date": birth,
        "birth_month": birth.month if birth else None,
        "birth_day": birth.day if birth else None,
        "birth_place": ", ".join(
            str(p) for p in (place.get("city"), place.get("state") or place.get("country")) if p
        )
        or None,
        "headshot_url": (athlete.get("headshot") or {}).get("href"),
        "active": (athlete.get("status") or {}).get("type") != "inactive",
    }


def _news_record(article: dict[str, Any], spec: LeagueSpec) -> dict[str, Any] | None:
    headline = article.get("headline")
    if not headline:
        return None
    categories = article.get("categories") or []
    return {
        "external_id": str(article.get("id") or headline)[:128],
        "league_key": spec.key,
        "headline": headline,
        "summary": article.get("description"),
        "type": (article.get("type") or "").lower(),
        "url": ((article.get("links") or {}).get("web") or {}).get("href"),
        "published_at": _parse_datetime(article.get("published")),
        "categories": [
            c.get("description") or c.get("type") for c in categories if isinstance(c, dict)
        ],
        "team_external_ids": [
            str((c.get("team") or {}).get("id"))
            for c in categories
            if isinstance(c, dict) and (c.get("team") or {}).get("id")
        ],
    }


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _parse_date(value: Any) -> date | None:
    parsed = _parse_datetime(value)
    return parsed.date() if parsed else None


def _month(on: date | None) -> int:
    return (on or datetime.now(UTC).date()).month


@register(PROVIDER_KEY)
def _build(settings: Settings) -> EspnProvider:
    return EspnProvider(settings)
