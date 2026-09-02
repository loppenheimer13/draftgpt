"""Building and publishing one family's daily show.

The pipeline, in order:

    brief -> write -> safety re-check -> optional narration -> persist -> publish

The safety re-check after writing is not redundant. The writer quotes headline
fragments, and the narration model rewrites sentences; both are places a
blocked phrase can re-enter copy that was clean when it was gathered. Checking
the finished script is the last gate before a child hears it, so it is the one
that must not be skipped.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from huddle.config import CALCULATION_VERSION, Settings, get_settings
from huddle.database.models import Family, ShowRun, ShowSegmentRow
from huddle.domain.enums import ShowStatus
from huddle.domain.safety import SafetyFilter
from huddle.show.brief import ShowBrief, build_brief
from huddle.show.narration import narrate
from huddle.show.writer import WrittenSegment, estimate_minutes, write_show
from huddle.yoto.auth import YotoAuthError, valid_access_token
from huddle.yoto.client import YotoClient
from huddle.yoto.labs import LabsClient
from huddle.yoto.publish import PublishResult, publish_show, voice_for

logger = logging.getLogger(__name__)


@dataclass
class BuildResult:
    run: ShowRun
    brief: ShowBrief
    segments: list[WrittenSegment] = field(default_factory=list)
    unchanged: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def estimated_minutes(self) -> float:
        return estimate_minutes(self.segments)


def build_show(
    session: Session,
    family: Family,
    *,
    show_date: date | None = None,
    now: datetime | None = None,
    settings: Settings | None = None,
    safety: SafetyFilter | None = None,
    force: bool = False,
) -> BuildResult:
    """Write today's show and persist it. Does not publish."""
    settings = settings or get_settings()
    safety = safety or SafetyFilter()
    now = now or datetime.now(UTC)
    local_date = show_date or now.astimezone(_zone(family.timezone)).date()

    brief = build_brief(session, family, show_date=local_date, now=now)
    run = _get_or_create_run(session, family, local_date, settings)
    run.status = str(ShowStatus.WRITING)
    run.started_at = now
    run.brief = brief.as_dict()
    run.blocked_stories = brief.blocked_stories

    brief_hash = brief.content_hash()
    if not force and run.brief_hash == brief_hash and run.status == ShowStatus.PUBLISHED:
        return BuildResult(run=run, brief=brief, unchanged=True)
    run.brief_hash = brief_hash

    written = write_show(
        brief,
        segments=family.enabled_segments,
        target_minutes=family.target_minutes,
        listener_age=family.listener_age,
    )
    if not written:
        run.status = str(ShowStatus.SKIPPED)
        run.error_message = "nothing to say today"
        session.flush()
        return BuildResult(run=run, brief=brief, warnings=["no segments produced"])

    warnings: list[str] = []
    narration_used = False

    # Replace the persisted segments wholesale: a rebuild is a fresh episode,
    # not an edit of yesterday's.
    for row in list(run.segments):
        session.delete(row)
    run.segments.clear()
    session.flush()

    for index, segment in enumerate(written, start=1):
        source_text = segment.text
        final_text, used, note = _polish(source_text, safety, settings)
        narration_used = narration_used or used
        if note:
            warnings.append(note)

        run.segments.append(
            ShowSegmentRow(
                segment=segment.segment,
                sort_order=index,
                chapter_key=f"{index:02d}",
                track_key="01",
                title=segment.title,
                host=segment.host,
                voice_id=voice_for(segment.host, family, settings),
                script=final_text,
                source_script=source_text if final_text != source_text else None,
                char_count=len(final_text),
                material_inputs=segment.material_inputs,
            )
        )

    run.narration_used = narration_used
    run.estimated_minutes = estimate_minutes(written)
    run.warnings = list({*(run.warnings or []), *warnings})
    run.status = str(ShowStatus.PENDING)
    session.flush()

    return BuildResult(run=run, brief=brief, segments=written, warnings=warnings)


def _polish(
    text: str, safety: SafetyFilter, settings: Settings
) -> tuple[str, bool, str | None]:
    """Optionally rewrite for charm, then re-check the result for safety.

    Order matters: narration runs first, the safety gate second. A rewrite that
    reintroduces a blocked phrase is discarded and the deterministic copy airs
    instead -- the show never goes out unchecked, and never fails to go out.
    """
    result = narrate(text, settings)
    candidate = result.text

    verdict = safety.check_text(candidate)
    if not verdict.allowed:
        if candidate != text:
            fallback = safety.check_text(text)
            if fallback.allowed:
                return text, False, (
                    f"narration reintroduced blocked wording ({verdict.category}); "
                    "used the written copy instead"
                )
        # Both the rewrite and the source trip the filter. That means the
        # writer produced something it should not have, which is a bug worth
        # surfacing loudly rather than quietly airing.
        logger.error("written copy failed its own safety check: %s", verdict.category)
        return (
            "We had a story here, but it wasn't quite right for the show, "
            "so we've left it out today.",
            False,
            f"segment replaced: written copy tripped the {verdict.category} filter",
        )

    return candidate, result.rewritten, None


def _get_or_create_run(
    session: Session, family: Family, show_date: date, settings: Settings
) -> ShowRun:
    run = session.scalar(
        select(ShowRun).where(
            ShowRun.family_id == family.id, ShowRun.show_date == show_date
        )
    )
    if run is None:
        run = ShowRun(family_id=family.id, show_date=show_date)
        session.add(run)
    run.calculation_version = CALCULATION_VERSION
    run.audio_pipeline = family.audio_pipeline or settings.audio_pipeline
    session.flush()
    return run


# -- publishing ------------------------------------------------------------
def publish_for_family(
    session: Session,
    family: Family,
    run: ShowRun,
    *,
    settings: Settings | None = None,
) -> PublishResult:
    """Send a built show to the family's Yoto card."""
    settings = settings or get_settings()
    credential = family.credential
    if credential is None:
        return PublishResult(
            status=str(ShowStatus.FAILED), error="family has not connected Yoto"
        )

    try:
        # A callable, not a string: a long publish can outlive one access token,
        # and this lets the client pick up a refreshed one between calls.
        def token() -> str:
            return valid_access_token(credential, settings)

        token()
    except YotoAuthError as exc:
        run.status = str(ShowStatus.FAILED)
        run.error_class = type(exc).__name__
        run.error_message = str(exc)[:2000]
        session.flush()
        return PublishResult(status=str(ShowStatus.FAILED), error=str(exc))

    with YotoClient(token, settings) as client, LabsClient(token, settings) as labs:
        result = publish_show(
            run, family, client=client, labs_client=labs, settings=settings
        )
    session.flush()
    return result


def run_daily(
    session: Session,
    *,
    now: datetime | None = None,
    settings: Settings | None = None,
    family_ids: list[str] | None = None,
    force: bool = False,
    publish: bool = True,
) -> list[tuple[Family, BuildResult, PublishResult | None]]:
    """Build and publish for every family whose local publish hour has arrived.

    One family's failure never stops the next: each is wrapped so a bad
    timezone, a revoked token, or a provider outage costs one household its
    show rather than everybody's.
    """
    settings = settings or get_settings()
    now = now or datetime.now(UTC)

    stmt = select(Family).where(Family.active.is_(True))
    if family_ids:
        stmt = stmt.where(Family.id.in_(family_ids))

    outcomes: list[tuple[Family, BuildResult, PublishResult | None]] = []
    for family in session.scalars(stmt):
        if not force and not _is_due(family, now):
            continue
        try:
            build = build_show(session, family, now=now, settings=settings, force=force)
            published = None
            if publish and build.segments and not build.unchanged:
                published = publish_for_family(session, family, build.run, settings=settings)
            outcomes.append((family, build, published))
            session.commit()
        except Exception as exc:  # noqa: BLE001 - one family must not break the rest
            session.rollback()
            logger.exception("show failed for family %s", family.id)
            outcomes.append((family, BuildResult(run=None, brief=None, warnings=[str(exc)]), None))  # type: ignore[arg-type]
    return outcomes


def _is_due(family: Family, now: datetime) -> bool:
    """True once the family's local publish hour has arrived and today's show
    has not already gone out."""
    local = now.astimezone(_zone(family.timezone))
    if local.hour < family.publish_hour:
        return False
    if family.last_published_at is None:
        return True
    last_local = family.last_published_at.replace(
        tzinfo=family.last_published_at.tzinfo or UTC
    ).astimezone(_zone(family.timezone))
    return last_local.date() < local.date()


def _zone(name: str | None):
    try:
        return ZoneInfo(name or "America/New_York")
    except Exception:  # noqa: BLE001
        return UTC
