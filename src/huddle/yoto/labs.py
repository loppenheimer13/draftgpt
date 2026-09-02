"""Yoto Labs: server-side text-to-speech.

The default pipeline. Huddle posts the script and Yoto synthesises it with
ElevenLabs and writes the card itself, so no audio ever crosses this process
and no ElevenLabs key is needed.

    POST https://labs.api.yotoplay.com/content/job?voiceId=<elevenlabs voice>

The job is asynchronous. Its status is what tells us whether this morning's
episode actually landed.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from huddle.config import Settings, get_settings
from huddle.yoto.client import YotoApiError

logger = logging.getLogger(__name__)

TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})


@dataclass
class LabsJob:
    job_id: str | None
    status: str
    total: int = 0
    completed: int = 0
    failed: int = 0
    card_id: str | None = None
    raw: dict[str, Any] | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def succeeded(self) -> bool:
        return self.status == "completed" and self.failed == 0


def _parse_job(payload: dict[str, Any]) -> LabsJob:
    job = payload.get("job") or payload
    progress = job.get("progress") or {}
    return LabsJob(
        job_id=job.get("jobId") or job.get("id"),
        status=str(job.get("status") or "queued"),
        total=int(progress.get("total") or 0),
        completed=int(progress.get("completed") or 0),
        failed=int(progress.get("failed") or 0),
        card_id=job.get("cardId") or payload.get("cardId"),
        raw=payload,
    )


class LabsClient:
    """Text-to-speech job submission. Shares the subscriber's access token."""

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

    def __enter__(self) -> LabsClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owned:
            self._http.close()

    @property
    def _base(self) -> str:
        return self.settings.yoto_labs_base.rstrip("/")

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token_provider()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def submit(self, content: dict[str, Any], *, voice_id: str | None = None) -> LabsJob:
        """Queue a synthesis job for a whole card.

        The voice is a default for the card; a track may override it with its
        own ``voiceId``.
        """
        voice = voice_id or self.settings.nova_voice_id
        url = f"{self._base}/content/job"
        try:
            response = self._http.post(
                url, json=content, params={"voiceId": voice}, headers=self._headers()
            )
        except httpx.HTTPError as exc:
            raise YotoApiError("POST", "/content/job", 0, str(exc)) from exc
        if response.status_code >= 400:
            raise YotoApiError("POST", "/content/job", response.status_code, response.text)
        return _parse_job(response.json() if response.content else {})

    def get_job(self, job_id: str) -> LabsJob:
        url = f"{self._base}/content/job/{job_id}"
        try:
            response = self._http.get(url, headers=self._headers())
        except httpx.HTTPError as exc:
            raise YotoApiError("GET", f"/content/job/{job_id}", 0, str(exc)) from exc
        if response.status_code >= 400:
            raise YotoApiError(
                "GET", f"/content/job/{job_id}", response.status_code, response.text
            )
        return _parse_job(response.json() if response.content else {})

    def wait_for_job(
        self,
        job_id: str,
        *,
        max_attempts: int = 60,
        poll_seconds: float = 3.0,
        sleep=time.sleep,
    ) -> LabsJob:
        """Poll until the job settles.

        A timeout is reported as ``timeout`` rather than raised: the job may
        well finish on Yoto's side afterwards, so the run records that it could
        not confirm the outcome instead of claiming a failure it did not see.
        """
        last = LabsJob(job_id=job_id, status="queued")
        for attempt in range(max_attempts):
            last = self.get_job(job_id)
            if last.is_terminal:
                return last
            if attempt + 1 < max_attempts:
                sleep(poll_seconds)
        logger.warning("labs job %s still %s after %s polls", job_id, last.status, max_attempts)
        return LabsJob(
            job_id=job_id, status="timeout", total=last.total,
            completed=last.completed, failed=last.failed, raw=last.raw,
        )
