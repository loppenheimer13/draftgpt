"""HTTP client for the Yoto API.

Scope of writes is deliberately narrow. This client creates and updates *one*
MYO card per subscriber and uploads the audio for it. It never sends a device
command, never changes a player's configuration, and never touches content it
did not create -- which is why Huddle can ask for content scopes alone.

Endpoints (https://api.yotoplay.com):
    GET    /content/mine
    GET    /content/{cardId}
    POST   /content
    DELETE /content/{cardId}
    GET    /media/transcode/audio/uploadUrl
    GET    /media/upload/{uploadId}/transcoded
    POST   /media/coverImage/user/me/upload
    POST   /media/displayIcons/user/me/upload
    GET    /media/displayIcons/user/yoto
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from huddle.config import Settings, get_settings

logger = logging.getLogger(__name__)

#: Yoto transcodes asynchronously. These bound the wait so a stuck upload fails
#: the run instead of hanging the scheduler.
TRANSCODE_POLL_SECONDS = 2.0
TRANSCODE_MAX_ATTEMPTS = 60


class YotoApiError(RuntimeError):
    """A Yoto API call failed. Carries status and a truncated body, no token."""

    def __init__(self, method: str, path: str, status: int, body: str = "") -> None:
        super().__init__(f"{method} {path} -> {status}: {body[:300]}")
        self.method = method
        self.path = path
        self.status = status
        self.body = body

    @property
    def is_auth_error(self) -> bool:
        return self.status in (401, 403)

    @property
    def is_retryable(self) -> bool:
        return self.status == 429 or self.status >= 500


@dataclass
class TranscodedAudio:
    """What the transcoder gives back, normalized into what a track needs."""

    sha256: str
    duration: float | None
    file_size: int | None
    channels: str | None
    audio_format: str | None
    raw: dict[str, Any]

    @property
    def track_url(self) -> str:
        """Yoto addresses transcoded audio by content hash, not by URL."""
        return f"yoto:#{self.sha256}"


class YotoClient:
    """Authenticated client bound to one subscriber's access token.

    ``token_provider`` is a callable rather than a string so a long publish can
    transparently pick up a refreshed token between calls.
    """

    def __init__(
        self,
        token_provider,
        settings: Settings | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._token_provider = token_provider if callable(token_provider) else (
            lambda: token_provider
        )
        self._owned = client is None
        self._http = client or httpx.Client(timeout=self.settings.http_timeout_seconds)

    def __enter__(self) -> YotoClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owned:
            self._http.close()

    # -- plumbing ----------------------------------------------------------
    @property
    def _base(self) -> str:
        return self.settings.yoto_api_base.rstrip("/")

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._token_provider()}",
            "Accept": "application/json",
        }
        if extra:
            headers.update(extra)
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: dict[str, Any] | None = None,
        retries: int = 2,
    ) -> Any:
        url = path if path.startswith("http") else f"{self._base}{path}"
        headers = self._headers({"Content-Type": "application/json"} if json_body else None)

        attempt = 0
        while True:
            try:
                response = self._http.request(
                    method, url, json=json_body, params=params, headers=headers
                )
            except httpx.HTTPError as exc:
                if attempt >= retries:
                    raise YotoApiError(method, path, 0, str(exc)) from exc
                attempt += 1
                time.sleep(2**attempt)
                continue

            if response.status_code >= 400:
                error = YotoApiError(method, path, response.status_code, response.text)
                # Auth failures are never retried here: the caller refreshes the
                # token and calls again, which is the only thing that can help.
                if error.is_retryable and attempt < retries:
                    attempt += 1
                    time.sleep(_retry_after(response, attempt))
                    continue
                raise error

            if not response.content:
                return None
            try:
                return response.json()
            except ValueError:
                return response.text

    # -- content -----------------------------------------------------------
    def list_my_content(self) -> list[dict[str, Any]]:
        """Every MYO card this user owns."""
        payload = self._request("GET", "/content/mine") or {}
        cards = payload.get("cards", payload) if isinstance(payload, dict) else payload
        return cards if isinstance(cards, list) else []

    def get_content(self, card_id: str) -> dict[str, Any]:
        return self._request("GET", f"/content/{card_id}") or {}

    def create_or_update_content(self, content: dict[str, Any]) -> dict[str, Any]:
        """Create a card, or update one when ``cardId`` is present.

        Reusing the card id is the whole point of the daily refresh: the
        physical MYO card in someone's hand keeps working, and only what it
        plays changes.
        """
        return self._request("POST", "/content", json_body=content) or {}

    def delete_content(self, card_id: str) -> None:
        self._request("DELETE", f"/content/{card_id}")

    # -- audio upload ------------------------------------------------------
    def get_upload_url(self, *, sha256: str | None = None) -> tuple[str, str]:
        """Reserve a one-time upload slot. Returns ``(uploadUrl, uploadId)``.

        Passing a known ``sha256`` lets Yoto short-circuit an upload it already
        has, which is why an unchanged segment costs nothing to republish.
        """
        params = {"sha256": sha256} if sha256 else None
        payload = self._request("GET", "/media/transcode/audio/uploadUrl", params=params) or {}
        upload = payload.get("upload") or {}
        upload_url, upload_id = upload.get("uploadUrl"), upload.get("uploadId")
        if not upload_url or not upload_id:
            raise YotoApiError("GET", "/media/transcode/audio/uploadUrl", 502, str(payload))
        return upload_url, upload_id

    def upload_audio(self, upload_url: str, audio: bytes, content_type: str = "audio/mpeg") -> None:
        """PUT the bytes to the reserved slot.

        No Authorization header: the URL is itself the credential, and sending
        the bearer token to a storage host would leak it outside the API.
        """
        try:
            response = self._http.put(
                upload_url, content=audio, headers={"Content-Type": content_type}
            )
        except httpx.HTTPError as exc:
            raise YotoApiError("PUT", "<upload-url>", 0, str(exc)) from exc
        if response.status_code >= 400:
            raise YotoApiError("PUT", "<upload-url>", response.status_code, response.text)

    def wait_for_transcode(
        self,
        upload_id: str,
        *,
        loudnorm: bool = False,
        max_attempts: int = TRANSCODE_MAX_ATTEMPTS,
        poll_seconds: float = TRANSCODE_POLL_SECONDS,
        sleep=time.sleep,
    ) -> TranscodedAudio:
        """Poll until the transcoder publishes a hash for the upload."""
        for attempt in range(max_attempts):
            payload = self._request(
                "GET",
                f"/media/upload/{upload_id}/transcoded",
                params={"loudnorm": str(loudnorm).lower()},
            ) or {}
            transcode = payload.get("transcode") or {}
            sha = transcode.get("transcodedSha256")
            if sha:
                info = transcode.get("transcodedInfo") or {}
                return TranscodedAudio(
                    sha256=sha,
                    duration=_as_float(info.get("duration")),
                    file_size=_as_int(info.get("fileSize")),
                    channels=info.get("channels"),
                    audio_format=info.get("format"),
                    raw=transcode,
                )
            if attempt + 1 < max_attempts:
                sleep(poll_seconds)
        raise YotoApiError(
            "GET", f"/media/upload/{upload_id}/transcoded", 504, "transcoding did not complete"
        )

    # -- images ------------------------------------------------------------
    def upload_cover_image(
        self, image: bytes, *, filename: str = "cover.png", content_type: str = "image/png"
    ) -> str | None:
        payload = self._post_file(
            "/media/coverImage/user/me/upload", image, filename, content_type
        )
        cover = (payload or {}).get("coverImage") or {}
        return cover.get("mediaUrl") or cover.get("url")

    def upload_display_icon(
        self, image: bytes, *, filename: str = "icon.png", content_type: str = "image/png"
    ) -> str | None:
        """Upload a 16x16 icon. Returns the ``yoto:#…`` reference for a track."""
        payload = self._post_file(
            "/media/displayIcons/user/me/upload",
            image,
            filename,
            content_type,
            params={"autoConvert": "true"},
        )
        icon = (payload or {}).get("displayIcon") or {}
        media_id = icon.get("mediaId")
        return f"yoto:#{media_id}" if media_id else None

    def public_icons(self) -> list[dict[str, Any]]:
        payload = self._request("GET", "/media/displayIcons/user/yoto") or {}
        icons = payload.get("displayIcons", payload) if isinstance(payload, dict) else payload
        return icons if isinstance(icons, list) else []

    def _post_file(
        self,
        path: str,
        data: bytes,
        filename: str,
        content_type: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        url = f"{self._base}{path}"
        try:
            response = self._http.post(
                url,
                files={"file": (filename, data, content_type)},
                params=params,
                headers=self._headers(),
            )
        except httpx.HTTPError as exc:
            raise YotoApiError("POST", path, 0, str(exc)) from exc
        if response.status_code >= 400:
            raise YotoApiError("POST", path, response.status_code, response.text)
        try:
            return response.json()
        except ValueError:
            return None


def _retry_after(response: httpx.Response, attempt: int) -> float:
    """Honour Retry-After when the server sends one, back off otherwise."""
    header = response.headers.get("Retry-After")
    if header:
        try:
            return min(float(header), 60.0)
        except ValueError:
            pass
    return float(2**attempt)


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
