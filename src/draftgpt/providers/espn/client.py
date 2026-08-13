"""Thin HTTP client for ESPN's read-only fantasy endpoints.

Read-only by construction: this client exposes GET only and never posts a
lineup, claim, or transaction. Private leagues require the ``espn_s2`` and
``SWID`` cookies from a logged-in browser session; those come from the
environment and are never logged.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from draftgpt.providers.base import ProviderAuthError, ProviderUnavailable

logger = logging.getLogger(__name__)

BASE_URL = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl"
PROVIDER_KEY = "espn"


class EspnClient:
    def __init__(
        self,
        league_id: str,
        season: int,
        cookies: dict[str, str] | None = None,
        timeout: float = 20.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.league_id = league_id
        self.season = season
        self._cookies = cookies or {}
        self._client = httpx.Client(
            timeout=timeout,
            transport=transport,
            headers={
                "Accept": "application/json",
                # ESPN rejects requests without a browser-like UA.
                "User-Agent": "draftgpt/0.1 (+read-only fantasy client)",
            },
            cookies=self._cookies,
            follow_redirects=True,
        )

    @property
    def is_authenticated(self) -> bool:
        return bool(self._cookies)

    def league_url(self) -> str:
        return f"{BASE_URL}/seasons/{self.season}/segments/0/leagues/{self.league_id}"

    def get(
        self,
        views: list[str] | str,
        *,
        params: dict[str, Any] | None = None,
        fantasy_filter: dict[str, Any] | None = None,
        path: str | None = None,
    ) -> Any:
        """Fetch one or more ESPN ``view``s.

        ``fantasy_filter`` becomes the ``x-fantasy-filter`` header, which is how
        ESPN scopes the player pool (limit, sort, availability status).
        """
        view_list = [views] if isinstance(views, str) else list(views)
        query: dict[str, Any] = dict(params or {})
        query["view"] = view_list

        headers: dict[str, str] = {}
        if fantasy_filter is not None:
            headers["x-fantasy-filter"] = json.dumps(fantasy_filter, separators=(",", ":"))

        url = path or self.league_url()
        try:
            response = self._client.get(url, params=query, headers=headers)
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(
                PROVIDER_KEY, ",".join(view_list), f"transport error: {type(exc).__name__}"
            ) from exc

        if response.status_code in (401, 403):
            raise ProviderAuthError(
                PROVIDER_KEY,
                ",".join(view_list),
                "not authorized -- private league requires valid DRAFTGPT_ESPN_S2 "
                "and DRAFTGPT_ESPN_SWID cookies",
            )
        if response.status_code == 404:
            raise ProviderUnavailable(
                PROVIDER_KEY,
                ",".join(view_list),
                f"league {self.league_id} not found for season {self.season}",
            )
        if response.status_code == 429:
            raise ProviderUnavailable(PROVIDER_KEY, ",".join(view_list), "rate limited")
        if response.status_code >= 500:
            raise ProviderUnavailable(
                PROVIDER_KEY, ",".join(view_list), f"upstream {response.status_code}"
            )
        if response.status_code >= 400:
            raise ProviderUnavailable(
                PROVIDER_KEY, ",".join(view_list), f"unexpected {response.status_code}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderUnavailable(
                PROVIDER_KEY, ",".join(view_list), "response was not valid JSON"
            ) from exc

        # Historical seasons return a single-element list rather than an object.
        if isinstance(payload, list) and len(payload) == 1:
            return payload[0]
        return payload

    def player_pool(
        self,
        *,
        limit: int = 400,
        status: str = "FREEAGENT",
        scoring_period_id: int | None = None,
    ) -> Any:
        """Fetch the player pool via ``kona_player_info``.

        ``status`` accepts ESPN's values: FREEAGENT, WAIVERS, ONTEAM, ALL.
        """
        status_filter = ["FREEAGENT", "WAIVERS"] if status == "FREEAGENT" else [status]
        fantasy_filter = {
            "players": {
                "filterStatus": {"value": status_filter} if status != "ALL" else {},
                "limit": limit,
                "sortPercOwned": {"sortAsc": False, "sortPriority": 1},
            }
        }
        params: dict[str, Any] = {}
        if scoring_period_id is not None:
            params["scoringPeriodId"] = scoring_period_id
        return self.get("kona_player_info", params=params, fantasy_filter=fantasy_filter)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> EspnClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
