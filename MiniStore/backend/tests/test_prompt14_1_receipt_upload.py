"""Prompt 14.1 Phase 5 Tests: Customer Receipt Upload, Ownership Isolation & Deletion."""

from __future__ import annotations

import base64
import struct
import uuid

from fastapi.testclient import TestClient

from app.db.session import get_sessionmaker
from app.models.entities import Customer, Order
from app.services.seed import DEFAULT_CUSTOMER_1_ID, DEFAULT_CUSTOMER_2_ID


def _make_png(width: int, height: int) -> str:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + ihdr + b"\x00\x00\x00\x00"
    return base64.b64encode(raw).decode("ascii")


def test_customer_receipt_claim_upload_ownership_safety_and_deletion(client: TestClient) -> None:
    """Customer uploads receipt on own order; other customer/bomb blocked; deletion works."""
    with get_sessionmaker()() as db:
        c1 = db.get(Customer, DEFAULT_CUSTOMER_1_ID)
        c2 = db.get(Customer, DEFAULT_CUSTOMER_2_ID)
        assert c1 is not None and c2 is not None
        t1 = c1.session_token
        t2 = c2.session_token

        order_id = f"ORD-RCPT-{uuid.uuid4().hex[:8].upper()}"
        db.add(
            Order(
                id=order_id,
                order_code=order_id[:20],
                merchant_id=c1.merchant_id,
                commerce_session_id=c1.commerce_session_id,
                customer_id=c1.id,
                status="PENDING_PAYMENT",
                subtotal_vnd=250000,
                discount_vnd=0,
                shipping_fee_vnd=0,
                total_vnd=250000,
                recipient_name=c1.display_name,
                recipient_phone=c1.phone,
                shipping_address=c1.default_address,
                idempotency_key=f"IDEMP-{order_id}",
                correlation_id=f"CORR-{order_id}",
            )
        )
        db.commit()

    # 1. Another customer CANNOT upload receipt on customer 1's order (404)
    r_other = client.post(
        f"/api/v1/store/orders/{order_id}/receipt-claim",
        headers={"X-Customer-Token": t2},
        json={
            "content_base64": _make_png(400, 300),
            "content_type": "image/png",
            "claimed_txn_ref": "MOCKTXN123",
            "claimed_amount_vnd": 250000,
        },
    )
    assert r_other.status_code == 404

    # 2. Decompression bomb image is rejected (400)
    r_bomb = client.post(
        f"/api/v1/store/orders/{order_id}/receipt-claim",
        headers={"X-Customer-Token": t1},
        json={
            "content_base64": _make_png(12000, 12000),
            "content_type": "image/png",
        },
    )
    assert r_bomb.status_code == 400

    # 3. Executable/unsupported MIME is rejected (400)
    r_exe = client.post(
        f"/api/v1/store/orders/{order_id}/receipt-claim",
        headers={"X-Customer-Token": t1},
        json={
            "content_base64": base64.b64encode(b"#!/bin/sh").decode("ascii"),
            "content_type": "application/x-sh",
        },
    )
    assert r_exe.status_code == 400

    # 4. Legitimate customer receipt upload succeeds and stores privately as CLAIM
    r_ok = client.post(
        f"/api/v1/store/orders/{order_id}/receipt-claim",
        headers={"X-Customer-Token": t1},
        json={
            "content_base64": _make_png(400, 300),
            "content_type": "image/png",
            "claimed_txn_ref": "MOCKTXN12345",
            "claimed_amount_vnd": 250000,
            "note": "[SYNTHETIC TEST] Paid via mock transfer",
        },
    )
    assert r_ok.status_code == 201
    body = r_ok.json()
    assert body["evidence_kind"] == "CLAIM"
    assert body["ocr_extraction_status"] == "UNAVAILABLE_MANUAL_REVIEW_REQUIRED"
    comp_id = body["complaint_id"]

    # 5. Customer retention/deletion removes private receipt file
    r_del = client.delete(
        f"/api/v1/store/orders/{order_id}/complaints/{comp_id}",
        headers={"X-Customer-Token": t1},
    )
    assert r_del.status_code == 200
    assert r_del.json()["deleted_files"] == 1
