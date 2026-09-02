"""Publishing a written show to a family's Yoto card.

Both pipelines end in the same place -- one MYO card, republished in place, so
the physical card on a shelf keeps playing today's episode without anyone
re-linking anything.

    labs   : POST the script; Yoto synthesises and writes the card.
    upload : synthesise locally, PUT the audio, wait for transcode, POST the card.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from huddle.config import Settings, get_settings
from huddle.domain.enums import Host, ShowStatus
from huddle.yoto.client import YotoApiError, YotoClient
from huddle.yoto.content import (
    LABS_TRACK_CHAR_LIMIT,
    build_content,
    chapters_from_segments,
    resolve_segment_icons,
)
from huddle.yoto.labs import LabsClient

logger = logging.getLogger(__name__)

CARD_TITLE = "Huddle: Your Daily Sports Show"


@dataclass
class PublishResult:
    status: str
    card_id: str | None = None
    job_id: str | None = None
    tracks: int = 0
    duration_seconds: float | None = None
    warnings: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == ShowStatus.PUBLISHED


def voice_for(host: str, family, settings: Settings) -> str:
    """The voice this host speaks with, preferring the family's own choice."""
    if str(host) == Host.RAE:
        return family.rae_voice_id or settings.rae_voice_id
    return family.nova_voice_id or settings.nova_voice_id


def card_title(family) -> str:
    """One stable title. The card id is what actually identifies it, but a
    stable title is what lets a first-run publish find the card it just made."""
    return CARD_TITLE


def publish_show(
    run,
    family,
    *,
    client: YotoClient,
    labs_client: LabsClient | None = None,
    synthesizer=None,
    settings: Settings | None = None,
    resolve_icons: bool = True,
) -> PublishResult:
    """Turn ``run``'s segments into audio on the family's card."""
    settings = settings or get_settings()
    if not run.segments:
        return PublishResult(status=ShowStatus.SKIPPED, error="show has no segments")

    pipeline = (run.audio_pipeline or family.audio_pipeline or settings.audio_pipeline).lower()
    chapters = chapters_from_segments(run.segments)
    if resolve_icons:
        resolve_segment_icons(client, chapters)

    description = _description(run)
    try:
        if pipeline == "labs":
            result = _publish_via_labs(
                run, family, chapters, description,
                labs_client=labs_client, client=client, settings=settings,
            )
        elif pipeline == "upload":
            result = _publish_via_upload(
                run, family, chapters, description,
                client=client, synthesizer=synthesizer, settings=settings,
            )
        else:
            return PublishResult(
                status=ShowStatus.FAILED, error=f"unknown audio pipeline '{pipeline}'"
            )
    except YotoApiError as exc:
        logger.warning("publish failed for family=%s: %s", family.id, exc)
        return PublishResult(status=ShowStatus.FAILED, error=str(exc))

    if result.card_id:
        family.card_id = result.card_id
        run.card_id = result.card_id
    if result.ok:
        family.last_published_at = datetime.now(UTC)
        run.published_at = family.last_published_at
    run.status = result.status
    run.labs_job_id = result.job_id or run.labs_job_id
    run.total_duration_seconds = result.duration_seconds
    if result.error:
        run.error_message = result.error[:2000]
    run.warnings = list({*(run.warnings or []), *result.warnings})
    return result


# -- labs ------------------------------------------------------------------
def _publish_via_labs(
    run, family, chapters, description, *, labs_client, client, settings
) -> PublishResult:
    if labs_client is None:
        raise ValueError("the labs pipeline requires a LabsClient")

    warnings: list[str] = []
    for chapter in chapters:
        for track in chapter.tracks:
            if len(track.script) > LABS_TRACK_CHAR_LIMIT:
                # The script builder splits to fit, so reaching here means a
                # segment slipped through; say so rather than truncate silently.
                warnings.append(
                    f"track '{track.title}' was truncated to the {LABS_TRACK_CHAR_LIMIT}"
                    " character Labs limit"
                )

    content = build_content(
        chapters,
        title=card_title(family),
        description=description,
        card_id=family.card_id,
        labs=True,
    )
    # The card-level voice is only a default; every track carries its own,
    # which is how the two hosts alternate.
    job = labs_client.submit(content, voice_id=voice_for(Host.NOVA, family, settings))
    if not job.job_id:
        return PublishResult(
            status=ShowStatus.FAILED, error="Labs did not return a job id", warnings=warnings
        )

    settled = labs_client.wait_for_job(job.job_id)
    card_id = settled.card_id or family.card_id
    track_count = sum(len(c.tracks) for c in chapters)

    if settled.status == "timeout":
        # Not a failure we witnessed. The next run reconciles by card id.
        warnings.append("Labs synthesis did not confirm completion before the poll window closed")
        return PublishResult(
            status=ShowStatus.SYNTHESIZING, card_id=card_id, job_id=job.job_id,
            tracks=track_count, warnings=warnings,
        )
    if not settled.succeeded:
        return PublishResult(
            status=ShowStatus.FAILED, card_id=card_id, job_id=job.job_id,
            tracks=track_count, warnings=warnings,
            error=f"Labs job {settled.status} ({settled.failed}/{settled.total} tracks failed)",
        )

    if card_id is None:
        # First publish: Labs made the card, so find the one it just wrote.
        card_id = _find_card_by_title(client, card_title(family))
        if card_id is None:
            warnings.append(
                "published, but the new card id could not be resolved; "
                "tomorrow's run will create a second card unless it is set manually"
            )

    return PublishResult(
        status=ShowStatus.PUBLISHED, card_id=card_id, job_id=job.job_id,
        tracks=track_count, warnings=warnings,
    )


def _find_card_by_title(client: YotoClient, title: str) -> str | None:
    """Newest MYO card with this exact title, if any."""
    try:
        cards = client.list_my_content()
    except YotoApiError:
        return None
    matches = [c for c in cards if (c.get("title") or "").strip() == title]
    if not matches:
        return None
    matches.sort(key=lambda c: str(c.get("updatedAt") or c.get("createdAt") or ""), reverse=True)
    return matches[0].get("cardId") or matches[0].get("id")


# -- upload ----------------------------------------------------------------
def _publish_via_upload(
    run, family, chapters, description, *, client, synthesizer, settings
) -> PublishResult:
    if synthesizer is None:
        from huddle.tts import get_synthesizer

        synthesizer = get_synthesizer(settings)

    warnings: list[str] = []
    segments_by_key = {(s.chapter_key, s.track_key): s for s in run.segments}

    for chapter in chapters:
        for track in chapter.tracks:
            audio = synthesizer.synthesize(
                track.script, voice_id=track.voice_id or voice_for(track.host, family, settings)
            )
            upload_url, upload_id = client.get_upload_url()
            client.upload_audio(upload_url, audio.audio, content_type=audio.content_type)
            transcoded = client.wait_for_transcode(upload_id)

            track.audio_sha256 = transcoded.sha256
            track.duration = transcoded.duration
            track.file_size = transcoded.file_size
            track.channels = transcoded.channels
            track.audio_format = transcoded.audio_format or audio.audio_format

            # Mirror onto the persisted segment so the run stays reproducible.
            segment = segments_by_key.get((chapter.key, track.key))
            if segment is not None:
                segment.audio_sha256 = transcoded.sha256
                segment.duration_seconds = transcoded.duration
                segment.file_size = transcoded.file_size
                segment.audio_format = track.audio_format

    content = build_content(
        chapters,
        title=card_title(family),
        description=description,
        card_id=family.card_id,
        labs=False,
    )
    response = client.create_or_update_content(content)
    card = response.get("card") or response
    card_id = card.get("cardId") or card.get("id") or family.card_id

    total = sum(t.duration or 0.0 for c in chapters for t in c.tracks)
    return PublishResult(
        status=ShowStatus.PUBLISHED,
        card_id=card_id,
        tracks=sum(len(c.tracks) for c in chapters),
        duration_seconds=round(total, 1) if total else None,
        warnings=warnings,
    )


def _description(run) -> str:
    return f"Your sports show for {run.show_date.isoformat()}, made by Huddle."
