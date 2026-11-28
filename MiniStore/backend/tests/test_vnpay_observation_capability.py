"""Read-only Guardian capability evidence follows commerce scope and provenance."""

from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from test_prompt11_1_payment_safety import _create_vnpay_order, _enable_vnpay

from app.api.service_read import service_payment_capabilities
from app.core.auth import ServicePrincipal
from app.core.config import get_settings
from app.db.session import get_sessionmaker
from app.models.entities import Order, Payment, PaymentQueryAttempt
from app.models.enums import ObservationProvenance


def test_vnpay_capability_excludes_fixture_and_out_of_scope_evidence(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_vnpay(monkeypatch)
    monkeypatch.setenv("COMMERCE_VNPAY_QUERYDR_ENABLED", "false")
    get_settings.cache_clear()
    order_id, payment_id, _, _ = _create_vnpay_order(client)

    with get_sessionmaker()() as db:
        order = db.get(Order, order_id)
        payment = db.get(Payment, payment_id)
        assert order is not None and payment is not None
        principal = ServicePrincipal(
            credential_id="capability-test",
            service_name="finresolve",
            merchant_id=order.merchant_id,
            commerce_session_id=order.commerce_session_id,
            world_epoch=order.world_epoch,
            allowed_scopes=frozenset(),
        )
        payment.signature_verified = True
        payment.observation_source = "VNPAY_IPN"
        payment.provider_environment = ObservationProvenance.TEST_FIXTURE.value
        db.flush()
        assert service_payment_capabilities(principal, db) == {
            "vnpay_configured": True,
            "vnpay_querydr_available": False,
            "vnpay_evidence_sources": [],
        }

        payment.provider_environment = ObservationProvenance.VNPAY_SANDBOX.value
        attempt = PaymentQueryAttempt(
            id="CAPABILITY-QUERY",
            payment_id=payment_id,
            order_id=order_id,
            merchant_id=order.merchant_id,
            provider="VNPAY_SANDBOX",
            request_id="CAPABILITY-QUERY",
            http_status=200,
            provider_response_code="99",
        )
        db.add(attempt)
        db.flush()
        assert service_payment_capabilities(principal, db)["vnpay_evidence_sources"] == [
            "VNPAY_IPN"
        ]

        attempt.provider_response_code = "00"
        attempt.signature_verified = True
        attempt.mapped_payment_status = "SUCCEEDED"
        db.flush()
        assert service_payment_capabilities(principal, db)["vnpay_evidence_sources"] == [
            "VNPAY_IPN",
            "VNPAY_QUERYDR",
        ]
        for other_scope in (
            replace(principal, merchant_id="OTHER-MERCHANT"),
            replace(principal, commerce_session_id="OTHER-SESSION"),
            replace(principal, world_epoch=principal.world_epoch + 1),
        ):
            assert service_payment_capabilities(other_scope, db)["vnpay_evidence_sources"] == []
