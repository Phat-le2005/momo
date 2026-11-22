"""SQLAlchemy engine/session factory (PostgreSQL is the source of truth)."""

from __future__ import annotations

from collections.abc import Iterator
from functools import lru_cache
from typing import Any

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings


def _connect_args(url: str, timeout: int) -> dict[str, Any]:
    if url.startswith("postgresql"):
        return {"connect_timeout": timeout}
    if url.startswith("sqlite"):
        return {"check_same_thread": False}
    return {}


@lru_cache
def get_engine() -> Engine:
    settings = get_settings()
    return create_engine(
        settings.database_url,
        pool_pre_ping=True,
        connect_args=_connect_args(settings.database_url, settings.db_connect_timeout_seconds),
    )


@lru_cache
def get_sessionmaker() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a request-scoped session."""
    session = get_sessionmaker()()
    try:
        yield session
    finally:
        session.close()


def check_database() -> bool:
    """Return True if a trivial query succeeds. Never raises."""
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001 - health probe must not crash
        return False
