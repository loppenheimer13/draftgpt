"""Engine and session management."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from draftgpt.config import get_settings

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def prepare_sqlite_path(url: str) -> None:
    """Create the parent directory for a file-backed SQLite database.

    Also called from the Alembic environment, which builds its own engine.
    """
    prefix = "sqlite:///"
    if not url.startswith(prefix):
        return
    path = url[len(prefix) :]
    if path and path != ":memory:":
        Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


_prepare_sqlite = prepare_sqlite_path


def get_engine(url: str | None = None) -> Engine:
    global _engine, _session_factory
    if _engine is not None and url is None:
        return _engine

    resolved = url or get_settings().database_url
    _prepare_sqlite(resolved)
    engine = create_engine(resolved, future=True, pool_pre_ping=True)

    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # type: ignore[no-untyped-def]
            cursor = dbapi_conn.cursor()
            # Foreign keys are OFF by default in SQLite; the schema depends on them.
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    if url is None:
        _engine = engine
        _session_factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    return engine


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        get_engine()
    assert _session_factory is not None
    return _session_factory


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope. Commits on success, rolls back on any exception."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_engine() -> None:
    """Test hook: drop cached engine/factory so a new URL takes effect."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None
