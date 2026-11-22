"""Scoped READ-only business tools querying actual PostgreSQL simulator/domain tables.

Hard Domain Invariants:
1. Strictly READ-ONLY: None of these tools mutate any database record.
2. Strictly Session-Scoped: Every query is bound to `self.session_id` validated by the server.
   Any `session_id` passed by model arguments is ignored/rejected.
3. Provenance & Freshness: Every tool result carries `source`, `source_ref`, `source_mode="MOCK"`,
   `observed_at`, and `freshness_ms`.
4. No Oracle Leakage: Only observable business and provider fields are returned.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from app.ai_core.schemas import ALLOWED_READ_TOOLS
from app.models.actions import ActionProposal
from app.models.enums import PaymentStatus, RefundStatus
from app.models.simulator import (
    InventoryLevel,
    InventoryMovement,
    InventoryReservation,
    OrderItem,
    Product,
    SimOrder,
    SimPayment,
    SimPaymentEvent,
    SimRefund,
    Warehouse,
)


class ToolResult(BaseModel):
    """Standardized read tool observation envelope with provenance metadata."""

    tool_name: str
    status: Literal["OK", "NOT_FOUND", "TIMEOUT", "ERROR"]
    source: str
    source_ref: str
    source_mode: str = "MOCK"
    observed_at: datetime
    freshness_ms: int = 0
    data: dict[str, Any] = Field(default_factory=dict)


class ReadToolRegistry:
    """Executes authorized read-only tools against the session-scoped database."""

    def __init__(self, db: Session, session_id: str) -> None:
        self._db = db
        self._session_id = session_id

    def execute(
        self,
        tool_name: str,
        args: dict[str, Any],
        *,
        simulate_timeout: bool = False,
    ) -> ToolResult:
        """Dispatch and execute an allowlisted read tool."""
        if tool_name not in ALLOWED_READ_TOOLS:
            raise ValueError(f"Tool '{tool_name}' is not in ALLOWED_READ_TOOLS")

        start_ns = time.perf_counter_ns()
        dispatch_map = {
            "get_order": self._get_order,
            "find_payment_candidates": self._find_payment_candidates,
            "get_payment_record": self._get_payment_record,
            "get_refund_history": self._get_refund_history,
            "get_inventory": self._get_inventory,
            "get_policy": self._get_policy,
            "get_order_events": self._get_order_events,
        }
        handler = dispatch_map[tool_name]
        result = handler(args, simulate_timeout=simulate_timeout)
        elapsed_ms = max(0, int((time.perf_counter_ns() - start_ns) / 1_000_000))
        result.freshness_ms = elapsed_ms
        return result

    def _resolve_order(self, args: dict[str, Any]) -> SimOrder | None:
        """Find a single SimOrder in the current session by exact ID, prefix, phone, or amount."""
        order_id = str(args.get("order_id") or "").strip()
        customer_phone = str(args.get("customer_phone") or "").strip()
        amount = args.get("amount")

        if order_id:
            # 1. Exact match within session
            exact = self._db.scalar(
                select(SimOrder)
                .options(selectinload(SimOrder.items))
                .where(SimOrder.session_id == self._session_id, SimOrder.id == order_id)
            )
            if exact is not None:
                return exact

            # 2. Prefix match within session (e.g. "ORD-SYNC" -> "ORD-SYNC-<suffix>")
            prefix_matches = self._db.scalars(
                select(SimOrder)
                .options(selectinload(SimOrder.items))
                .where(
                    SimOrder.session_id == self._session_id,
                    SimOrder.id.ilike(f"{order_id}%"),
                )
                .order_by(SimOrder.created_at.asc())
            ).all()
            if len(prefix_matches) == 1:
                return prefix_matches[0]

        if customer_phone:
            by_phone = self._db.scalars(
                select(SimOrder)
                .options(selectinload(SimOrder.items))
                .where(
                    SimOrder.session_id == self._session_id,
                    SimOrder.customer_phone == customer_phone,
                )
                .order_by(SimOrder.created_at.asc())
            ).all()
            if len(by_phone) == 1:
                return by_phone[0]

        if isinstance(amount, int) and amount > 0:
            by_amount = self._db.scalars(
                select(SimOrder)
                .options(selectinload(SimOrder.items))
                .where(
                    SimOrder.session_id == self._session_id,
                    SimOrder.amount == amount,
                )
                .order_by(SimOrder.created_at.asc())
            ).all()
            if len(by_amount) == 1:
                return by_amount[0]

        return None

    def _get_order(self, args: dict[str, Any], *, simulate_timeout: bool = False) -> ToolResult:
        now = datetime.now(UTC)
        order = self._resolve_order(args)
        requested_ref = str(
            args.get("order_id") or args.get("customer_phone") or args.get("amount") or "query"
        )

        if order is None:
            # Also list available session orders metadata if query had no specific match
            return ToolResult(
                tool_name="get_order",
                status="NOT_FOUND",
                source="SIM_ORDERS_DB",
                source_ref=f"sim_orders:{requested_ref}",
                source_mode="MOCK",
                observed_at=now,
                data={
                    "found": False,
                    "queried_ref": requested_ref,
                    "message": f"Không tìm thấy đơn hàng khớp với '{requested_ref}' trong phiên.",
                },
            )

        items_data = [
            {
                "item_id": item.id,
                "product_id": item.product_id,
                "sku": item.sku_snapshot,
                "product_name": item.product_name_snapshot,
                "quantity": item.quantity,
                "unit_price": item.unit_price,
                "discount": item.discount,
                "tax": item.tax,
                "line_total": item.line_total,
                "currency": item.currency,
            }
            for item in order.items
        ]

        return ToolResult(
            tool_name="get_order",
            status="OK",
            source="SIM_ORDERS_DB",
            source_ref=f"sim_orders:{order.id}",
            source_mode=order.source_mode,
            observed_at=now,
            data={
                "found": True,
                "order_id": order.id,
                "customer_name": order.customer_name,
                "customer_phone": order.customer_phone,
                "amount": order.amount,
                "currency": order.currency,
                "subtotal": order.subtotal,
                "discount": order.discount,
                "tax": order.tax,
                "shipping": order.shipping,
                "grand_total": order.grand_total,
                "status": order.status,
                "payment_status": order.payment_status,
                "fulfillment_status": order.fulfillment_status,
                "version": order.version,
                "source_mode": order.source_mode,
                "items": items_data,
            },
        )

    def _find_payment_candidates(
        self, args: dict[str, Any], *, simulate_timeout: bool = False
    ) -> ToolResult:
        now = datetime.now(UTC)
        order_id_arg = str(args.get("order_id") or "").strip()
        provider_ref_arg = str(args.get("provider_ref") or "").strip()
        amount_arg = args.get("amount")

        # QA T04: Support provider timeout simulation
        if (
            simulate_timeout
            or args.get("simulate_timeout") is True
            or "TIMEOUT" in order_id_arg.upper()
            or "TIMEOUT" in provider_ref_arg.upper()
        ):
            return ToolResult(
                tool_name="find_payment_candidates",
                status="TIMEOUT",
                source="PROVIDER_LEDGER_MOCK",
                source_ref=f"provider_query:{order_id_arg or provider_ref_arg or 'timeout'}",
                source_mode="MOCK",
                observed_at=now,
                data={
                    "timed_out": True,
                    "outcome": "UNKNOWN",
                    "message": (
                        "Truy vấn nhà cung cấp thanh toán bị quá thời gian (TIMEOUT). "
                        "Trạng thái giao dịch là UNKNOWN; tuyệt đối không tự động xác nhận."
                    ),
                },
            )

        # Resolve order_id if a prefix like "ORD-SYNC" was passed
        resolved_order_id = order_id_arg
        if order_id_arg:
            resolved_order = self._resolve_order({"order_id": order_id_arg})
            if resolved_order is not None:
                resolved_order_id = resolved_order.id

        conditions = []
        if resolved_order_id:
            conditions.append(SimPayment.order_id == resolved_order_id)
        if provider_ref_arg:
            conditions.append(SimPayment.provider_ref == provider_ref_arg)
            # Support suffix matching for user short refs like MOMO_TXN_888 -> MOMO_TXN_<suffix>_888
            if "_" in provider_ref_arg:
                parts = provider_ref_arg.split("_")
                prefix_part = "_".join(parts[:2])
                tail_part = parts[-1]
                conditions.append(SimPayment.provider_ref.ilike(f"{prefix_part}%{tail_part}"))
        if isinstance(amount_arg, int) and amount_arg > 0:
            conditions.append(SimPayment.amount == amount_arg)

        stmt = select(SimPayment).where(SimPayment.session_id == self._session_id)
        if conditions:
            # Prefer exact order_id match if available, otherwise OR across search criteria
            if resolved_order_id:
                exact_order_payments = self._db.scalars(
                    select(SimPayment)
                    .where(
                        SimPayment.session_id == self._session_id,
                        SimPayment.order_id == resolved_order_id,
                    )
                    .order_by(SimPayment.created_at.asc())
                ).all()
                if exact_order_payments:
                    payments = list(exact_order_payments)
                else:
                    payments = list(
                        self._db.scalars(
                            stmt.where(or_(*conditions)).order_by(SimPayment.created_at.asc())
                        ).all()
                    )
            else:
                payments = list(
                    self._db.scalars(
                        stmt.where(or_(*conditions)).order_by(SimPayment.created_at.asc())
                    ).all()
                )
        else:
            payments = []

        # Check if any candidate record signals a simulated gateway timeout (result_code 504)
        for p in payments:
            if p.result_code == 504 or "TIMEOUT" in (p.provider_ref or "").upper():
                return ToolResult(
                    tool_name="find_payment_candidates",
                    status="TIMEOUT",
                    source="PROVIDER_LEDGER_MOCK",
                    source_ref=f"sim_payments:{p.id}",
                    source_mode=p.source_mode,
                    observed_at=now,
                    data={
                        "timed_out": True,
                        "payment_id": p.id,
                        "outcome": "UNKNOWN",
                        "message": (
                            "Cổng thanh toán phản hồi TIMEOUT (504). "
                            "Kết quả là UNKNOWN, không được phép ghi nhận thành công."
                        ),
                    },
                )

        candidates_list = [
            {
                "payment_id": p.id,
                "order_id": p.order_id,
                "merchant_id": p.merchant_id,
                "provider": p.provider,
                "provider_ref": p.provider_ref,
                "provider_order_id": p.provider_order_id,
                "request_id": p.request_id,
                "result_code": p.result_code,
                "amount": p.amount,
                "currency": p.currency,
                "status": p.status,
                "verified_at": p.verified_at.isoformat() if p.verified_at else None,
                "verification_source": p.verification_source,
                "authoritative_success": (
                    p.status == PaymentStatus.SUCCESS.value and p.verified_at is not None
                ),
                "source_mode": p.source_mode,
            }
            for p in payments
        ]

        # QA T03: Two or more candidate transactions -> mark as ambiguous!
        is_ambiguous = len(candidates_list) > 1
        ref_label = resolved_order_id or provider_ref_arg or str(amount_arg or "candidates")

        return ToolResult(
            tool_name="find_payment_candidates",
            status="OK" if candidates_list else "NOT_FOUND",
            source="PROVIDER_LEDGER_MOCK",
            source_ref=f"sim_payments:search:{ref_label}",
            source_mode="MOCK",
            observed_at=now,
            data={
                "queried_order_id": resolved_order_id or None,
                "queried_provider_ref": provider_ref_arg or None,
                "candidate_count": len(candidates_list),
                "is_ambiguous": is_ambiguous,
                "candidates": candidates_list,
            },
        )

    def _get_payment_record(
        self, args: dict[str, Any], *, simulate_timeout: bool = False
    ) -> ToolResult:
        now = datetime.now(UTC)
        payment_id = str(args.get("payment_id") or "").strip()
        provider_ref = str(args.get("provider_ref") or "").strip()

        if simulate_timeout or args.get("simulate_timeout") is True:
            return ToolResult(
                tool_name="get_payment_record",
                status="TIMEOUT",
                source="PROVIDER_LEDGER_MOCK",
                source_ref=f"sim_payments:{payment_id or provider_ref}",
                source_mode="MOCK",
                observed_at=now,
                data={"timed_out": True, "outcome": "UNKNOWN"},
            )

        payment: SimPayment | None = None
        if payment_id:
            payment = self._db.scalar(
                select(SimPayment).where(
                    SimPayment.session_id == self._session_id,
                    SimPayment.id == payment_id,
                )
            )
        if payment is None and provider_ref:
            payment = self._db.scalar(
                select(SimPayment).where(
                    SimPayment.session_id == self._session_id,
                    SimPayment.provider_ref == provider_ref,
                )
            )

        if payment is None:
            return ToolResult(
                tool_name="get_payment_record",
                status="NOT_FOUND",
                source="PROVIDER_LEDGER_MOCK",
                source_ref=f"sim_payments:{payment_id or provider_ref or 'unknown'}",
                source_mode="MOCK",
                observed_at=now,
                data={
                    "found": False,
                    "payment_id": payment_id or None,
                    "provider_ref": provider_ref or None,
                    "authoritative_success": False,
                },
            )

        if payment.result_code == 504 or "TIMEOUT" in (payment.provider_ref or "").upper():
            return ToolResult(
                tool_name="get_payment_record",
                status="TIMEOUT",
                source="PROVIDER_LEDGER_MOCK",
                source_ref=f"sim_payments:{payment.id}",
                source_mode=payment.source_mode,
                observed_at=now,
                data={"timed_out": True, "payment_id": payment.id, "outcome": "UNKNOWN"},
            )

        events = self._db.scalars(
            select(SimPaymentEvent)
            .where(
                SimPaymentEvent.session_id == self._session_id,
                SimPaymentEvent.payment_id == payment.id,
            )
            .order_by(SimPaymentEvent.created_at.asc())
        ).all()

        authoritative_success = (
            payment.status == PaymentStatus.SUCCESS.value and payment.verified_at is not None
        )

        return ToolResult(
            tool_name="get_payment_record",
            status="OK",
            source="PROVIDER_LEDGER_MOCK",
            source_ref=f"sim_payments:{payment.id}",
            source_mode=payment.source_mode,
            observed_at=now,
            data={
                "found": True,
                "payment_id": payment.id,
                "order_id": payment.order_id,
                "merchant_id": payment.merchant_id,
                "provider": payment.provider,
                "provider_ref": payment.provider_ref,
                "provider_order_id": payment.provider_order_id,
                "request_id": payment.request_id,
                "result_code": payment.result_code,
                "amount": payment.amount,
                "currency": payment.currency,
                "status": payment.status,
                "verified_at": payment.verified_at.isoformat() if payment.verified_at else None,
                "verification_source": payment.verification_source,
                "authoritative_success": authoritative_success,
                "source_mode": payment.source_mode,
                "events_count": len(events),
            },
        )

    def _get_refund_history(
        self, args: dict[str, Any], *, simulate_timeout: bool = False
    ) -> ToolResult:
        now = datetime.now(UTC)
        payment_id = str(args.get("payment_id") or "").strip()
        order_id_arg = str(args.get("order_id") or "").strip()

        resolved_order_id = order_id_arg
        if order_id_arg:
            resolved_order = self._resolve_order({"order_id": order_id_arg})
            if resolved_order is not None:
                resolved_order_id = resolved_order.id

        payment: SimPayment | None = None
        if payment_id:
            payment = self._db.scalar(
                select(SimPayment).where(
                    SimPayment.session_id == self._session_id,
                    SimPayment.id == payment_id,
                )
            )
        elif resolved_order_id:
            payment = self._db.scalar(
                select(SimPayment)
                .where(
                    SimPayment.session_id == self._session_id,
                    SimPayment.order_id == resolved_order_id,
                )
                .order_by(SimPayment.created_at.asc())
            )

        conditions = []
        if payment is not None:
            conditions.append(SimRefund.payment_id == payment.id)
        if resolved_order_id:
            conditions.append(SimRefund.order_id == resolved_order_id)

        if conditions:
            refunds = list(
                self._db.scalars(
                    select(SimRefund)
                    .where(SimRefund.session_id == self._session_id, or_(*conditions))
                    .order_by(SimRefund.created_at.asc())
                ).all()
            )
        else:
            refunds = []

        payment_amount = payment.amount if payment is not None else 0
        total_completed = sum(
            r.completed_amount for r in refunds if r.status == RefundStatus.COMPLETED.value
        )
        total_pending_or_unknown = sum(
            r.requested_amount
            for r in refunds
            if r.status in (RefundStatus.PENDING.value, RefundStatus.UNKNOWN.value)
        )
        remaining_refundable = max(0, payment_amount - total_completed - total_pending_or_unknown)

        refund_items = [
            {
                "refund_id": r.id,
                "payment_id": r.payment_id,
                "order_id": r.order_id,
                "amount": r.amount,
                "requested_amount": r.requested_amount,
                "approved_amount": r.approved_amount,
                "completed_amount": r.completed_amount,
                "currency": r.currency,
                "status": r.status,
                "reason": r.reason,
                "approval_ref": r.approval_ref,
                "provider_refund_id": r.provider_refund_id,
                "source_mode": r.source_mode,
            }
            for r in refunds
        ]

        ref_target = (payment.id if payment else None) or resolved_order_id or "none"
        return ToolResult(
            tool_name="get_refund_history",
            status="OK",
            source="SIM_REFUNDS_DB",
            source_ref=f"sim_refunds:{ref_target}",
            source_mode="MOCK",
            observed_at=now,
            data={
                "payment_id": payment.id if payment else None,
                "order_id": resolved_order_id or (payment.order_id if payment else None),
                "payment_amount": payment_amount,
                "payment_status": payment.status if payment else "NOT_FOUND",
                "refund_count": len(refund_items),
                "total_completed_refunds": total_completed,
                "total_pending_or_unknown_refunds": total_pending_or_unknown,
                "remaining_refundable_amount": remaining_refundable,
                "has_completed_refund": total_completed > 0,
                "has_pending_or_unknown_refund": total_pending_or_unknown > 0,
                "is_fully_refunded_or_locked": (payment is not None and remaining_refundable <= 0),
                "refunds": refund_items,
            },
        )

    def _get_inventory(self, args: dict[str, Any], *, simulate_timeout: bool = False) -> ToolResult:
        now = datetime.now(UTC)
        product_id = str(args.get("product_id") or "").strip()
        sku = str(args.get("sku") or "").strip()
        order_id_arg = str(args.get("order_id") or "").strip()

        product_ids: list[str] = []
        if product_id:
            product_ids.append(product_id)

        if sku:
            prod_by_sku = self._db.scalars(
                select(Product).where(
                    Product.session_id == self._session_id,
                    Product.sku.ilike(f"{sku}%"),
                )
            ).all()
            product_ids.extend(p.id for p in prod_by_sku)

        if order_id_arg:
            resolved_order = self._resolve_order({"order_id": order_id_arg})
            if resolved_order is not None:
                items = self._db.scalars(
                    select(OrderItem).where(
                        OrderItem.session_id == self._session_id,
                        OrderItem.order_id == resolved_order.id,
                    )
                ).all()
                product_ids.extend(i.product_id for i in items)

        stmt = select(InventoryLevel).where(InventoryLevel.session_id == self._session_id)
        if product_ids:
            stmt = stmt.where(InventoryLevel.product_id.in_(product_ids))

        levels = self._db.scalars(stmt).all()
        products_map = {
            p.id: p
            for p in self._db.scalars(
                select(Product).where(Product.session_id == self._session_id)
            ).all()
        }
        warehouses_map = {
            w.id: w
            for w in self._db.scalars(
                select(Warehouse).where(Warehouse.session_id == self._session_id)
            ).all()
        }

        inventory_records = []
        for lvl in levels:
            prod = products_map.get(lvl.product_id)
            wh = warehouses_map.get(lvl.warehouse_id)
            inventory_records.append(
                {
                    "inventory_id": lvl.id,
                    "product_id": lvl.product_id,
                    "sku": prod.sku if prod else "UNKNOWN",
                    "product_name": prod.name if prod else "UNKNOWN",
                    "warehouse_id": lvl.warehouse_id,
                    "warehouse_code": wh.code if wh else "UNKNOWN",
                    "on_hand": lvl.on_hand,
                    "reserved": lvl.reserved,
                    "available": lvl.on_hand - lvl.reserved,
                    "version": lvl.version,
                    "source_mode": lvl.source_mode,
                }
            )

        return ToolResult(
            tool_name="get_inventory",
            status="OK" if inventory_records else "NOT_FOUND",
            source="SIM_INVENTORY_DB",
            source_ref=f"inventory_levels:{order_id_arg or sku or product_id or 'all'}",
            source_mode="MOCK",
            observed_at=now,
            data={
                "count": len(inventory_records),
                "levels": inventory_records,
            },
        )

    def _get_policy(self, args: dict[str, Any], *, simulate_timeout: bool = False) -> ToolResult:
        now = datetime.now(UTC)
        return ToolResult(
            tool_name="get_policy",
            status="OK",
            source="POLICY_REGISTRY",
            source_ref="policy:finresolve-v1.2",
            source_mode="MOCK",
            observed_at=now,
            data={
                "policy_version": "FinResolve-Policy-v1.2",
                "authority_hierarchy": [
                    "1. Verified provider/merchant ledger (authorized and scoped)",
                    "2. Own business DB and versioned policy",
                    "3. Tool observations with provenance",
                    "4. User text/images (UNTRUSTED CLAIM)",
                    "5. AI hypothesis",
                ],
                "rules": {
                    "ORDER_SYNC": {
                        "require_verified_provider_payment": True,
                        "required_payment_status": "SUCCESS",
                        "require_amount_and_currency_match": True,
                        "require_unique_payment_match": True,
                        "disallow_from_customer_claim": True,
                    },
                    "REFUND_EXECUTE": {
                        "require_verified_provider_payment": True,
                        "include_completed_pending_and_unknown_in_balance": True,
                        "disallow_exceeding_remaining_balance": True,
                        "require_approval_role": "FINANCE_LEAD",
                        "on_duplicate_or_over_limit": "HOLD_ACTION_OR_REVIEW",
                    },
                    "AMBIGUOUS_PAYMENT_CANDIDATES": {
                        "action": "ASK_USER",
                        "target_case_state": "WAITING_EVIDENCE",
                        "rule": "Never randomly pick among multiple candidate transactions.",
                    },
                    "PROVIDER_TIMEOUT": {
                        "action": "STOP_OR_ESCALATE",
                        "target_case_state": "UNKNOWN_OUTCOME",
                        "rule": "Never retry writes blindly or mark RESOLVED on timeout.",
                    },
                    "RISK_RESPONSE": {
                        "allowed_responses": ["OBSERVE", "REVIEW", "HOLD_ACTION"],
                        "rule": "Risk signal is not proof of fraud; never accuse customer.",
                    },
                },
            },
        )

    def _get_order_events(
        self, args: dict[str, Any], *, simulate_timeout: bool = False
    ) -> ToolResult:
        now = datetime.now(UTC)
        order = self._resolve_order(args)
        order_id_ref = order.id if order else str(args.get("order_id") or "unknown")

        if order is None:
            return ToolResult(
                tool_name="get_order_events",
                status="NOT_FOUND",
                source="SIM_EVENTS_DB",
                source_ref=f"order_events:{order_id_ref}",
                source_mode="MOCK",
                observed_at=now,
                data={"found": False, "order_id": order_id_ref, "events": []},
            )

        payments = self._db.scalars(
            select(SimPayment).where(
                SimPayment.session_id == self._session_id,
                SimPayment.order_id == order.id,
            )
        ).all()
        payment_ids = [p.id for p in payments]

        pay_events = (
            list(
                self._db.scalars(
                    select(SimPaymentEvent)
                    .where(
                        SimPaymentEvent.session_id == self._session_id,
                        SimPaymentEvent.payment_id.in_(payment_ids),
                    )
                    .order_by(SimPaymentEvent.created_at.asc())
                ).all()
            )
            if payment_ids
            else []
        )

        reservations = self._db.scalars(
            select(InventoryReservation)
            .where(
                InventoryReservation.session_id == self._session_id,
                InventoryReservation.order_id == order.id,
            )
            .order_by(InventoryReservation.created_at.asc())
        ).all()

        reservation_ids = [r.id for r in reservations]
        movements = (
            list(
                self._db.scalars(
                    select(InventoryMovement)
                    .where(
                        InventoryMovement.session_id == self._session_id,
                        InventoryMovement.source_ref.in_(reservation_ids),
                    )
                    .order_by(InventoryMovement.created_at.asc())
                ).all()
            )
            if reservation_ids
            else []
        )

        proposals = self._db.scalars(
            select(ActionProposal)
            .where(ActionProposal.session_id == self._session_id)
            .order_by(ActionProposal.created_at.asc())
        ).all()
        order_proposals = [pr for pr in proposals if pr.params_json.get("order_id") == order.id]

        return ToolResult(
            tool_name="get_order_events",
            status="OK",
            source="SIM_EVENTS_DB",
            source_ref=f"order_events:{order.id}",
            source_mode="MOCK",
            observed_at=now,
            data={
                "found": True,
                "order_id": order.id,
                "payment_events": [
                    {
                        "id": pe.id,
                        "payment_id": pe.payment_id,
                        "event_type": pe.event_type,
                        "provider": pe.provider,
                        "request_id": pe.request_id,
                        "created_at": pe.created_at.isoformat(),
                    }
                    for pe in pay_events
                ],
                "reservations": [
                    {
                        "id": r.id,
                        "product_id": r.product_id,
                        "warehouse_id": r.warehouse_id,
                        "quantity": r.quantity,
                        "status": r.status,
                    }
                    for r in reservations
                ],
                "movements": [
                    {
                        "id": m.id,
                        "product_id": m.product_id,
                        "delta": m.delta,
                        "reason": m.reason,
                        "source_ref": m.source_ref,
                    }
                    for m in movements
                ],
                "existing_proposals": [
                    {
                        "proposal_id": pr.id,
                        "action_type": pr.action_type,
                        "status": pr.status,
                    }
                    for pr in order_proposals
                ],
            },
        )
