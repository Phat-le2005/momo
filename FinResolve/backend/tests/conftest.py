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
os.environ["APP_ENV"] = "test"
os.environ["DATABASE_URL"] = TEST_DB_URL
os.environ["WEB_ORIGIN"] = "http://localhost:3000"
os.environ["SOURCE_MODE"] = "MOCK"
for _key in ("MODEL_PROVIDER", "MODEL_NAME", "MODEL_API_KEY"):
    os.environ[_key] = ""


def _clear_caches() -> None:
    from app.core.config import get_settings
    from app.core.rate_limit import reset_rate_limiter
    from app.db.session import get_engine, get_sessionmaker

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    reset_rate_limiter()


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
                        "inventory_reservations, inventory_movements, sim_payment_events, "
                        "supervisor_states, supervisor_jobs, challenges, "
                        "supervisor_activities, supervisor_workers, "
                        "normalized_events, llm_concurrency_leases, "
                        "commerce_source_bindings, anomaly_observations CASCADE"
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
    """Helper fixture to create an active demo session (public route returns guest token only)."""
    from app.core.session import issue_demo_manager_token
    from app.db.session import get_sessionmaker
    from app.models.session import DemoSession

    res = client.post("/api/v1/demo/sessions", json={"ttl_minutes": 60, "max_cases": 10})
    assert res.status_code == 201
    data = res.json()
    assert "manager_token" not in data

    maker = get_sessionmaker()
    with maker() as db:
        sess_obj = db.get(DemoSession, data["session_id"])
        assert sess_obj is not None
        mgr_token = issue_demo_manager_token(sess_obj, role="DEMO_MANAGER")

    return {
        "id": data["session_id"],
        "token": data["token"],
        "manager_token": mgr_token,
    }
