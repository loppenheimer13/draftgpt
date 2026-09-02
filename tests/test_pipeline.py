"""End-to-end: brief -> write -> safety re-check -> persist."""

from __future__ import annotations

from datetime import timedelta

from conftest import add_game, add_story
from huddle.domain.enums import ShowStatus
from huddle.show.narration import introduces_facts, narrate
from huddle.show.runner import build_show


def test_build_produces_a_playable_show(db, family, followed, now, settings) -> None:
    add_game(db, followed, start_at=now - timedelta(hours=12), state="post",
             home=True, home_score=6, away_score=2)
    add_story(db, headline="Mets top the Phillies 4-3 in extras", verdict="allow")

    result = build_show(db, family, now=now, settings=settings)
    assert result.run.status == ShowStatus.PENDING
    assert result.run.segments, "a show with content must produce tracks"
    assert result.estimated_minutes > 0

    for row in result.run.segments:
        assert row.script.strip()
        assert row.char_count == len(row.script)
        assert row.host in ("nova", "rae")
        assert row.voice_id, "every track needs a voice or it cannot be synthesised"


def test_chapters_are_ordered_and_uniquely_keyed(db, family, followed, now, settings) -> None:
    """Chapter keys drive the physical skip buttons; duplicates break them."""
    result = build_show(db, family, now=now, settings=settings)
    keys = [row.chapter_key for row in result.run.segments]
    assert keys == sorted(keys)
    assert len(keys) == len(set(keys))


def test_rebuilding_replaces_rather_than_appends(db, family, followed, now, settings) -> None:
    first = build_show(db, family, now=now, settings=settings)
    count = len(first.run.segments)
    second = build_show(db, family, now=now, settings=settings, force=True)
    assert len(second.run.segments) == count


def test_blocked_stories_are_recorded_on_the_run(db, family, followed, now, settings) -> None:
    story = add_story(db, headline="Player arrested overnight", verdict="block")
    story.safety_category = "legal"
    db.flush()
    result = build_show(db, family, now=now, settings=settings)
    assert any(item["reason"] == "legal" for item in result.run.blocked_stories)


def test_a_family_with_nothing_still_gets_a_show(db, family, now, settings) -> None:
    """No teams, no news, mid-summer: the curated segments must carry it."""
    result = build_show(db, family, now=now, settings=settings)
    assert result.run.segments
    assert result.run.status == ShowStatus.PENDING


def test_written_copy_is_re_checked_for_safety(db, family, followed, now, settings) -> None:
    """The last gate. Copy that was clean when gathered can be rewritten into
    something that is not, so the finished script is checked again."""
    from huddle.domain.safety import SafetyFilter

    result = build_show(db, family, now=now, settings=settings)
    filt = SafetyFilter()
    for row in result.run.segments:
        assert filt.check_text(row.script).allowed, f"unsafe copy aired: {row.script[:80]}"


# -- narration guardrail ---------------------------------------------------
def test_narration_is_off_by_default(settings) -> None:
    result = narrate("The Braves won.", settings)
    assert result.rewritten is False
    assert result.text == "The Braves won."


def test_rewrite_may_not_introduce_a_number() -> None:
    original = "Bijan Robinson is questionable. He played 72 percent of snaps."
    assert introduces_facts(original, "Bijan Robinson is questionable at 72 percent.") is None
    assert "85" in (introduces_facts(original, "He played 85 percent of snaps.") or "")


def test_rewrite_may_not_introduce_a_person() -> None:
    """The failure this guard exists for: inventing a player who was never
    mentioned, stated to a child as fact."""
    reason = introduces_facts(
        "The Braves won five to three.", "The Braves won five to three, said Brian Snitker."
    )
    assert reason is not None and "Snitker" in reason


def test_rewrite_may_reword_freely_within_the_facts() -> None:
    original = "The Braves beat the Nationals five to three."
    assert introduces_facts(original, "The Braves got past the Nationals, five to three!") is None


def test_narration_failure_never_blocks_a_show(settings, monkeypatch) -> None:
    """Narration is polish. It must not be able to stop an episode."""
    settings.narration_enabled = True
    settings.anthropic_api_key = "sk-test"

    class Boom:
        class messages:
            @staticmethod
            def create(**_kw):
                raise RuntimeError("model unavailable")

    result = narrate("The Braves won.", settings, client=Boom())
    assert result.text == "The Braves won."
    assert result.rewritten is False
    assert "api error" in (result.reason or "")
