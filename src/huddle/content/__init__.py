"""Hand-written editorial content: On This Day moments and Rookie Factoids.

None of this comes from an API. It is checked by hand, written for a young
listener, and versioned with the code -- which is what lets the show carry two
segments a day that are guaranteed safe and guaranteed interesting even when
every league in the catalogue is out of season.

Selection is deterministic on the date, so every household hears the same
moment on the same morning, and a family that listens daily does not get the
same factoid twice in a row.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path
from random import Random
from typing import Any

logger = logging.getLogger(__name__)

CONTENT_DIR = Path(__file__).resolve().parent
MOMENTS_FILE = CONTENT_DIR / "on_this_day.yaml"
FACTOIDS_FILE = CONTENT_DIR / "factoids.yaml"


@dataclass(frozen=True)
class Moment:
    title: str
    story: str
    league: str | None = None
    month: int | None = None
    day: int | None = None

    @property
    def is_dated(self) -> bool:
        """True when this really happened on today's date, rather than being
        pulled in to fill a day with no entry. The host says so either way --
        claiming an anniversary that is not one would be a small lie told to a
        child every morning."""
        return self.month is not None and self.day is not None


@dataclass(frozen=True)
class Factoid:
    question: str
    answer: str
    kind: str = "rules"
    sport: str | None = None


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        logger.warning("content file missing: %s", path)
        return {}
    try:
        import yaml

        return yaml.safe_load(path.read_text()) or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not parse %s: %s", path, exc)
        return {}


@lru_cache(maxsize=1)
def load_moments() -> tuple[Moment, ...]:
    data = _load_yaml(MOMENTS_FILE)
    return tuple(
        Moment(
            title=entry["title"],
            story=entry["story"],
            league=entry.get("league"),
            month=entry.get("month"),
            day=entry.get("day"),
        )
        for entry in data.get("moments", [])
        if entry.get("title") and entry.get("story")
    )


@lru_cache(maxsize=1)
def load_factoids() -> tuple[Factoid, ...]:
    data = _load_yaml(FACTOIDS_FILE)
    return tuple(
        Factoid(
            question=entry["question"],
            answer=entry["answer"],
            kind=entry.get("kind", "rules"),
            sport=entry.get("sport"),
        )
        for entry in data.get("factoids", [])
        if entry.get("question") and entry.get("answer")
    )


def reset_cache() -> None:
    """Test hook: re-read the content files."""
    load_moments.cache_clear()
    load_factoids.cache_clear()


def moment_for(on: date) -> Moment | None:
    """Today's moment: the real anniversary if there is one, else a rotation.

    The fallback is seeded by the date rather than randomised, so the choice is
    reproducible -- a parent asking "where did that come from?" can be given
    the same answer tomorrow.
    """
    moments = load_moments()
    if not moments:
        return None

    dated = [m for m in moments if m.month == on.month and m.day == on.day]
    if dated:
        return _pick(dated, on, salt="moment-exact")

    undated = [
        Moment(title=m.title, story=m.story, league=m.league) for m in moments
    ]
    return _pick(undated, on, salt="moment-rotation")


def factoid_for(on: date, *, exclude_kinds: tuple[str, ...] = ()) -> Factoid | None:
    pool = [f for f in load_factoids() if f.kind not in exclude_kinds]
    if not pool:
        return None
    return _pick(pool, on, salt="factoid")


def _pick(items: list, on: date, *, salt: str):
    """Deterministic choice keyed on the date, with no near-term repeats.

    An independent hash per day would be simplest, but with a pool of a few
    dozen entries the birthday paradox makes a repeat within two weeks likely
    -- and a child who hears the same "did you know" twice in a fortnight has
    noticed the seams.

    So the pool is shuffled once per year into a fixed permutation and walked
    by day of year. Every household hears the same entry on the same day, and
    nothing repeats until the whole pool has been used.
    """
    order = list(range(len(items)))
    seed = hashlib.sha256(f"{salt}:{on.year}".encode()).hexdigest()
    Random(seed).shuffle(order)
    return items[order[(on.timetuple().tm_yday - 1) % len(items)]]
