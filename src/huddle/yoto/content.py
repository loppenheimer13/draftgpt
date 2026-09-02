"""Building the Yoto card payload from digest segments.

A card is ``{title, content: {chapters}, metadata}``. Each chapter holds one or
more tracks, and a track is one audio file addressed by content hash -- or, on
the Labs path, the script text itself with ``type: "elevenlabs"``.

Huddle maps one show segment to one chapter and one track. That is what makes
the physical buttons useful: pressing left and right on the player skips
between Your Teams, Birthday Club and On This Day rather than scrubbing through
one long recording -- which is how a five-year-old navigates audio.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from huddle.domain.enums import SEGMENT_TITLES

#: Yoto Labs synthesises at most this many characters per track.
LABS_TRACK_CHAR_LIMIT = 3000

#: Keywords used to pick a stock Yoto icon per segment, matched case-insensitively
#: against the public icon catalogue. No hash is hard-coded: an icon we cannot
#: find resolves to null, which the schema allows, rather than to a wrong image.
SEGMENT_ICON_KEYWORDS: dict[str, tuple[str, ...]] = {
    "your_teams": ("shield", "flag", "trophy", "team"),
    "today_in_sports": ("newspaper", "news", "megaphone", "microphone"),
    "birthday_club": ("birthday", "cake", "balloon", "party"),
    "on_this_day": ("calendar", "clock", "history", "book"),
    "rookie_factoid": ("lightbulb", "idea", "question", "star"),
    "weekend_edition": ("calendar", "television", "popcorn", "watch"),
}


@dataclass
class TrackDraft:
    """One track before it knows whether it will be audio or Labs text."""

    key: str
    title: str
    #: The words to speak. Kept even on the upload path, for the transcript.
    script: str
    segment: str
    #: Which host reads it, and the voice that host uses today.
    host: str = "nova"
    voice_id: str | None = None
    audio_sha256: str | None = None
    duration: float | None = None
    file_size: int | None = None
    channels: str | None = None
    audio_format: str | None = None
    icon: str | None = None
    overlay_label: str | None = None


@dataclass
class ChapterDraft:
    key: str
    title: str
    segment: str
    tracks: list[TrackDraft] = field(default_factory=list)
    icon: str | None = None
    overlay_label: str | None = None


def chapters_from_segments(segments) -> list[ChapterDraft]:
    """Group ordered rows into one chapter per show segment.

    Chapter and track keys are positional two-digit strings, renumbered from
    scratch each day so a segment that produced nothing leaves no gap in the
    player's chapter list.
    """
    chapters: list[ChapterDraft] = []
    by_segment: dict[str, ChapterDraft] = {}

    for segment in sorted(segments, key=lambda s: s.sort_order):
        chapter = by_segment.get(segment.segment)
        if chapter is None:
            index = len(chapters) + 1
            chapter = ChapterDraft(
                key=f"{index:02d}",
                title=SEGMENT_TITLES.get(
                    segment.segment, segment.segment.replace("_", " ").title()
                ),
                segment=segment.segment,
                overlay_label=str(index),
            )
            by_segment[segment.segment] = chapter
            chapters.append(chapter)

        track_index = len(chapter.tracks) + 1
        chapter.tracks.append(
            TrackDraft(
                key=f"{track_index:02d}",
                title=segment.title,
                script=segment.script,
                segment=segment.segment,
                host=segment.host,
                voice_id=segment.voice_id,
                audio_sha256=segment.audio_sha256,
                duration=segment.duration_seconds,
                file_size=segment.file_size,
                audio_format=segment.audio_format,
                icon=segment.icon,
                overlay_label=str(track_index),
            )
        )
    return chapters


def build_content(
    chapters: list[ChapterDraft],
    *,
    title: str,
    description: str | None = None,
    card_id: str | None = None,
    labs: bool = False,
    cover_url: str | None = None,
) -> dict[str, Any]:
    """Assemble the ``POST /content`` (or Labs job) body.

    ``card_id`` turns this from a create into an update, which is how the same
    physical card gets new audio every morning.
    """
    payload_chapters = [
        _chapter_payload(chapter, labs=labs) for chapter in chapters if chapter.tracks
    ]

    total_duration = sum(
        track.duration or 0.0 for chapter in chapters for track in chapter.tracks
    )
    total_size = sum(track.file_size or 0 for chapter in chapters for track in chapter.tracks)

    metadata: dict[str, Any] = {"title": title}
    if description:
        metadata["description"] = description
    if cover_url:
        metadata["cover"] = {"imageL": cover_url}
    # The upload path knows real durations; Labs computes them after synthesis,
    # so advertising zeroes there would be worse than saying nothing.
    if not labs and total_duration:
        metadata["media"] = {
            "duration": round(total_duration),
            "fileSize": total_size,
            "readableFileSize": round(total_size / 1024 / 1024, 1),
        }

    content: dict[str, Any] = {
        "title": title,
        "content": {"chapters": payload_chapters},
        "metadata": metadata,
    }
    if card_id:
        content["cardId"] = card_id
    return content


def _chapter_payload(chapter: ChapterDraft, *, labs: bool) -> dict[str, Any]:
    return {
        "key": chapter.key,
        "title": chapter.title,
        "overlayLabel": chapter.overlay_label or chapter.key,
        "display": {"icon16x16": chapter.icon},
        "tracks": [_track_payload(track, labs=labs) for track in chapter.tracks],
    }


def _track_payload(track: TrackDraft, *, labs: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "key": track.key,
        "title": track.title,
        "overlayLabel": track.overlay_label or track.key,
        "display": {"icon16x16": track.icon},
    }
    if labs:
        # On the Labs path the script *is* the trackUrl; Yoto synthesises it.
        payload["type"] = "elevenlabs"
        payload["trackUrl"] = track.script[:LABS_TRACK_CHAR_LIMIT]
        # Per-track voice is what makes two hosts possible: the card-level
        # voiceId is only a default, and each track may override it.
        if track.voice_id:
            payload["voiceId"] = track.voice_id
        return payload

    if not track.audio_sha256:
        raise ValueError(f"track {track.key} has no transcoded audio to point at")
    payload.update(
        {
            "type": "audio",
            "trackUrl": f"yoto:#{track.audio_sha256}",
            "format": track.audio_format or "aac",
            "duration": track.duration,
            "fileSize": track.file_size,
        }
    )
    if track.channels:
        payload["channels"] = track.channels
    return payload


def resolve_segment_icons(client, chapters: list[ChapterDraft]) -> None:
    """Best-effort: label each chapter with a stock Yoto icon.

    Purely cosmetic, so any failure is swallowed -- a digest with plain icons
    is a working digest, and a card that fails to publish over an icon lookup
    would not be.
    """
    try:
        catalogue = client.public_icons()
    except Exception:  # noqa: BLE001 - cosmetic path, never fatal
        return

    index: list[tuple[str, str]] = []
    for icon in catalogue:
        media_id = icon.get("mediaId") or icon.get("displayIconId")
        title = " ".join(
            str(part) for part in (icon.get("title"), *(icon.get("publicTags") or ())) if part
        ).lower()
        if media_id and title:
            index.append((title, f"yoto:#{media_id}"))

    for chapter in chapters:
        for keyword in SEGMENT_ICON_KEYWORDS.get(chapter.segment, ()):
            match = next((ref for title, ref in index if keyword in title), None)
            if match:
                chapter.icon = match
                for track in chapter.tracks:
                    track.icon = track.icon or match
                break
