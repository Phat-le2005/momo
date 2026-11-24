"""Pytest fixtures for independent FinShop LIVE Commerce PostgreSQL tests."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

TEST_DB_URL = os.environ.get(
    "COMMERCE_TEST_DATABASE_URL",
    "postgresql+psycopg://finshop:finshop@127.0.0.1:5544/finshop_commerce_test",
)
os.environ["COMMERCE_APP_ENV"] = "test"
os.environ["COMMERCE_DATABASE_URL"] = TEST_DB_URL
os.environ["COMMERCE_WEB_ORIGIN"] = "http://localhost:3100"
os.environ["COMMERCE_SOURCE_MODE"] = "MOCK"


def _clear_caches() -> None:
    from app.core.config import get_settings
    from app.core.rate_limit import reset_rate_limiter
    from app.db.session import get_engine, get_sessionmaker

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    reset_rate_limiter()


@pytest.fixture(autouse=True)
def clean_commerce_db() -> Iterator[None]:
    _clear_caches()
    from app.db.session import get_engine, get_sessionmaker
    from app.services.seed import seed_commerce_data

    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE merchants, commerce_sessions, customers, products, product_variants, "
                "inventories, inventory_reservations, carts, cart_items, vouchers, "
                "voucher_redemptions, orders, order_items, payments, refund_requests, "
                "refund_transactions, live_sessions, live_products, live_comments, "
                "complaints, commerce_events, event_outbox CASCADE"
            )
        )
    with get_sessionmaker()() as db:
        seed_commerce_data(db)
    yield
    _clear_caches()


@pytest.fixture
def client() -> Iterator[TestClient]:
    from app.main import create_app

    with TestClient(create_app()) as tc:
        yield tc
