"""
Database connection. The URL comes from `paths.database` in config.toml:
PostgreSQL on an installed ggNet (the installer sets it up, over its Unix
socket: postgresql+psycopg:///ggnet?host=/run/postgresql), SQLite for
development and the tests.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_config


def database_url() -> str:
    return get_config()["paths"]["database"]


def make_engine(url: str | None = None) -> Engine:
    url = url or database_url()
    parsed = make_url(url)
    if parsed.get_backend_name() == "sqlite" and parsed.database not in (None, "", ":memory:"):
        # SQLite creates the file but not its directory.
        Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)

    # PostgreSQL may restart (package upgrade) under a running ggNet: test a
    # pooled connection before handing it out.
    engine = create_engine(url, pool_pre_ping=parsed.get_backend_name() == "postgresql")
    if engine.dialect.name == "sqlite":
        # Without this SQLite IGNORES foreign key constraints.
        @event.listens_for(engine, "connect")
        def _fk_on(dbapi_conn, _record):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()
    return engine


_SessionLocal: sessionmaker[Session] | None = None


def get_sessionmaker() -> sessionmaker[Session]:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=make_engine(), expire_on_commit=False)
    return _SessionLocal


def get_db() -> Iterator[Session]:
    """FastAPI dependency: one session per request."""
    db = get_sessionmaker()()
    try:
        yield db
    finally:
        db.close()
