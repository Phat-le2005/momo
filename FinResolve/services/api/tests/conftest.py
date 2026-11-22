"""Test fixtures and database configuration for tests."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://finresolve@127.0.0.1:5544/finresolve_test",
)
os.environ["DATABASE_URL"] = TEST_DB_URL
os.environ["WEB_ORIGIN"] = "http://localhost:3000"
os.environ["SOURCE_MODE"] = "MOCK"
for _key in ("MODEL_PROVIDER", "MODEL_NAME", "MODEL_API_KEY"):
    os.environ[_key] = ""


def _clear_caches() -> None:
    from app.core.config import get_settings
    from app.db.session import get_engine, get_sessionmaker

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()


@pytest.fixture(autouse=True)
def clean_database() -> Iterator[None]:
    """Clean database state between tests."""
    _clear_caches()
    from app.db.base import Base
    from app.db.session import get_engine

    engine = get_engine()
    if engine.dialect.name == "postgresql":
        try:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "TRUNCATE demo_sessions, cases, evidence, case_events, "
                        "action_proposals, approvals, action_executions, verifications, "
                        "risk_signals, sim_orders, sim_payments, sim_refunds, "
                        "products, warehouses, inventory_levels, order_items, "
                        "inventory_reservations, inventory_movements, sim_payment_events CASCADE"
                    )
                )
        except Exception:
            Base.metadata.create_all(bind=engine)
    elif engine.dialect.name == "sqlite":
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)

    yield

    _clear_caches()


@pytest.fixture
def client() -> Iterator[TestClient]:
    _clear_caches()
    from app.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client
    _clear_caches()


@pytest.fixture
def active_session(client: TestClient) -> dict[str, str]:
    """Helper fixture to create an active demo session."""
    res = client.post("/api/v1/demo/sessions", json={"ttl_minutes": 60, "max_cases": 10})
    assert res.status_code == 201
    data = res.json()
    return {"id": data["session_id"], "token": data["token"]}
