"""Deterministic Risk Engine for Transaction and Refund Safety (Prompt 04).

Hard Domain Invariants:
1. Provider/merchant ledger-backed refundable amount:
   `refundable_remainder = max(0, eligible_amount - settled_refunds - pending_or_held_refunds)`.
   Never negative, guards against partial, duplicate, and cumulative over-refund.
2. Categories: `ERROR`, `ANOMALY`, `SUSPECTED_ABUSE`, `CONFIRMED_POLICY_VIOLATION`.
3. Responses: `OBSERVE`, `REVIEW`, `HOLD_ACTION` (scoped to actions controlled by FinResolve).
4. Every RiskSignal creates a `CaseEvent(type="RISK_SIGNAL_DETECTED")` with a non-accusatory
   Vietnamese explanation and authoritative source refs. Never accuses customer of proven fraud.
5. Detects:
   - Missing or unverified authoritative payment
   - Mismatched order, amount, currency, merchant, or non-MOCK source_mode
   - Same transaction / refund request reuse (`request_id` collision)
   - Already refunded payment (QA T07)
   - In-flight pending or unknown refund (QA T08)
   - Partial or cumulative refund exceeding remaining eligible balance (QA T09)
   - Expired/stale approval (QA T11, T12) and order version change before write (QA T13)
   - Ambiguous claim requiring human review (QA T21)
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.cases.state_machine import can_transition, transition_case
from app.models.actions import ActionProposal
from app.models.cases import Case, CaseEvent, Evidence
from app.models.enums import (
    ActionProposalStatus,
    ActionType,
    CaseState,
    EvidenceKind,
    FulfillmentStatus,
    OrderPaymentStatus,
    OrderStatus,
    PaymentStatus,
    RefundStatus,
    RiskCategory,
    RiskLevel,
    RiskResponse,
)
from app.models.risk import RiskSignal
from app.models.simulator import SimOrder, SimPayment, SimRefund


@dataclass(frozen=True)
class RefundBalanceLedger:
    """Authoritative provider/merchant ledger balance breakdown for a payment."""

    payment_id: str | None
    order_id: str | None
    currency: str
    payment_status: str
    payment_verified: bool
    eligible_amount: int
    settled_refunds: int
    pending_or_held_refunds: int
    refundable_remainder: int
    refund_records_count: int
    source_refs: list[str] = field(default_factory=list)


@dataclass
class DetectedRiskItem:
    category: str
    level: str
    response: str
    rule_code: str
    explanation_vi: str
    evidence_refs: list[str]
    source_refs: list[str]
    details: dict[str, Any]
    persisted_signal_id: str | None = None


@dataclass
class RefundRiskEvaluation:
    allowed: bool
    recommended_response: str
    ledger: RefundBalanceLedger
    requested_amount: int
    explanation_vi: str
    signals: list[DetectedRiskItem] = field(default_factory=list)


RISK_RULES_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "rule_code": "MISSING_OR_UNVERIFIED_PAYMENT",
        "category": RiskCategory.ERROR.value,
        "severity": RiskLevel.HIGH.value,
        "default_response": RiskResponse.HOLD_ACTION.value,
        "description_vi": (
            "Không tìm thấy bản ghi thanh toán gốc hoặc giao dịch chưa ở trạng thái SUCCESS "
            "đã xác thực từ cổng thanh toán."
        ),
        "known_blind_spots": (
            "Trường hợp webhook từ ngân hàng trễ hơn cửa sổ đồng bộ cần truy vấn đối soát lại."
        ),
    },
    {
        "rule_code": "ORDER_PAYMENT_MISMATCH",
        "category": RiskCategory.ERROR.value,
        "severity": RiskLevel.HIGH.value,
        "default_response": RiskResponse.HOLD_ACTION.value,
        "description_vi": (
            "Bản ghi thanh toán không thuộc đơn hàng mục tiêu, hoặc sai lệch merchant_id, "
            "loại tiền tệ (currency), result_code hay source_mode."
        ),
        "known_blind_spots": (
            "Khách hàng nhập nhầm mã đơn của một giao dịch khác có cùng mệnh giá."
        ),
    },
    {
        "rule_code": "DUPLICATE_REFUND_REQUEST_ID",
        "category": RiskCategory.SUSPECTED_ABUSE.value,
        "severity": RiskLevel.HIGH.value,
        "default_response": RiskResponse.HOLD_ACTION.value,
        "description_vi": (
            "Mã yêu cầu hoàn tiền (request_id) hoặc tham số giao dịch hoàn tiền bị tái sử dụng "
            "trùng lặp trên một bản ghi hoàn tiền khác."
        ),
        "known_blind_spots": (
            "Có thể do lỗi mạng phía client gửi lặp với mã khóa khác; chỉ tạm giữ hành động "
            "(HOLD_ACTION), không kết luận khách hàng gian lận."
        ),
    },
    {
        "rule_code": "ALREADY_REFUNDED_PAYMENT",
        "category": RiskCategory.CONFIRMED_POLICY_VIOLATION.value,
        "severity": RiskLevel.HIGH.value,
        "default_response": RiskResponse.HOLD_ACTION.value,
        "description_vi": (
            "Giao dịch thanh toán đã được hoàn tiền toàn bộ (settled_refunds >= eligible_amount); "
            "số dư khả dụng bằng 0 VND."
        ),
        "known_blind_spots": (
            "Khách hàng có thể chưa nhận được SMS biến động số dư dù lệnh hoàn đã tất toán."
        ),
    },
    {
        "rule_code": "IN_FLIGHT_PENDING_REFUND",
        "category": RiskCategory.ANOMALY.value,
        "severity": RiskLevel.HIGH.value,
        "default_response": RiskResponse.HOLD_ACTION.value,
        "description_vi": (
            "Giao dịch đang có lệnh hoàn tiền ở trạng thái PENDING hoặc UNKNOWN chờ đối soát; "
            "không tạo thêm lệnh hoàn tiền chồng lấn."
        ),
        "known_blind_spots": (
            "Cổng thanh toán bên thứ ba chưa phản hồi trạng thái cuối của lệnh hoàn trước đó."
        ),
    },
    {
        "rule_code": "CUMULATIVE_REFUND_LIMIT_EXCEEDED",
        "category": RiskCategory.CONFIRMED_POLICY_VIOLATION.value,
        "severity": RiskLevel.HIGH.value,
        "default_response": RiskResponse.HOLD_ACTION.value,
        "description_vi": (
            "Số tiền đề xuất hoàn vượt quá số dư hoàn tiền khả dụng còn lại "
            "(eligible_amount - settled_refunds - pending_or_held_refunds)."
        ),
        "known_blind_spots": (
            "Các khoản bồi thường/voucher ngoài giao dịch gốc không được hoàn qua cổng thanh toán."
        ),
    },
    {
        "rule_code": "STALE_APPROVAL_OR_ORDER_VERSION",
        "category": RiskCategory.ANOMALY.value,
        "severity": RiskLevel.HIGH.value,
        "default_response": RiskResponse.HOLD_ACTION.value,
        "description_vi": (
            "Chữ ký phê duyệt đã hết hạn, sai lệch digest, hoặc phiên bản đơn hàng (order.version) "
            "đã thay đổi trước thời điểm ghi."
        ),
        "known_blind_spots": (
            "Nhân viên vận hành khác vừa cập nhật trạng thái đơn hàng song song."
        ),
    },
    {
        "rule_code": "AMBIGUOUS_CLAIM_NARRATIVE",
        "category": RiskCategory.ANOMALY.value,
        "severity": RiskLevel.MEDIUM.value,
        "default_response": RiskResponse.REVIEW.value,
        "description_vi": (
            "Thông tin khiếu nại có điểm mơ hồ hoặc nhiều giao dịch ứng viên; cần nhân viên "
            "xem xét (REVIEW), tuyệt đối không quy kết gian lận."
        ),
        "known_blind_spots": (
            "Hệ thống MVP không chấm điểm ML gian lận ngân hàng hoặc kiểm định ảnh vật lý."
        ),
    },
)


class RiskEngine:
    """Deterministic transaction and refund risk evaluator."""

    def __init__(self, db: Session, session_id: str) -> None:
        self._db = db
        self._session_id = session_id

    def compute_refundable_balance(
        self,
        *,
        payment_id: str | None = None,
        order_id: str | None = None,
        lock_rows: bool = False,
    ) -> tuple[RefundBalanceLedger, SimPayment | None, SimOrder | None]:
        """Compute authoritative refundable balance across settled and pending refunds."""
        payment: SimPayment | None = None
        order: SimOrder | None = None

        if order_id:
            stmt_ord = select(SimOrder).where(
                SimOrder.id == order_id,
                SimOrder.session_id == self._session_id,
            )
            if lock_rows:
                stmt_ord = stmt_ord.with_for_update()
            order = self._db.scalar(stmt_ord)

        if payment_id:
            stmt_pay = select(SimPayment).where(
                SimPayment.id == payment_id,
                SimPayment.session_id == self._session_id,
            )
            if lock_rows:
                stmt_pay = stmt_pay.with_for_update()
            payment = self._db.scalar(stmt_pay)
        elif order is not None:
            stmt_pay_ord = (
                select(SimPayment)
                .where(
                    SimPayment.order_id == order.id,
                    SimPayment.session_id == self._session_id,
                )
                .order_by(SimPayment.created_at.asc())
            )
            if lock_rows:
                stmt_pay_ord = stmt_pay_ord.with_for_update()
            payment = self._db.scalar(stmt_pay_ord)

        if payment is not None and order is None and payment.order_id:
            stmt_ord2 = select(SimOrder).where(
                SimOrder.id == payment.order_id,
                SimOrder.session_id == self._session_id,
            )
            if lock_rows:
                stmt_ord2 = stmt_ord2.with_for_update()
            order = self._db.scalar(stmt_ord2)

        if payment is None:
            return (
                RefundBalanceLedger(
                    payment_id=payment_id,
                    order_id=order.id if order else order_id,
                    currency=order.currency if order else "VND",
                    payment_status="NOT_FOUND",
                    payment_verified=False,
                    eligible_amount=0,
                    settled_refunds=0,
                    pending_or_held_refunds=0,
                    refundable_remainder=0,
                    refund_records_count=0,
                    source_refs=[f"sim_orders:{order.id}"] if order else [],
                ),
                None,
                order,
            )

        stmt_ref = (
            select(SimRefund)
            .where(
                SimRefund.payment_id == payment.id,
                SimRefund.session_id == self._session_id,
            )
            .order_by(SimRefund.created_at.asc())
        )
        if lock_rows:
            stmt_ref = stmt_ref.with_for_update()
        refunds = list(self._db.scalars(stmt_ref).all())

        is_verified = (
            payment.status == PaymentStatus.SUCCESS.value
            and payment.verified_at is not None
            and payment.result_code == 0
            and payment.source_mode == "MOCK"
        )
        eligible_amount = int(payment.amount) if is_verified else 0
        settled_refunds = sum(
            int(r.completed_amount) for r in refunds if r.status == RefundStatus.COMPLETED.value
        )
        pending_or_held_refunds = sum(
            int(r.requested_amount)
            for r in refunds
            if r.status in (RefundStatus.PENDING.value, RefundStatus.UNKNOWN.value)
        )
        refundable_remainder = max(0, eligible_amount - settled_refunds - pending_or_held_refunds)

        source_refs = [f"sim_payments:{payment.id}"]
        if order is not None:
            source_refs.append(f"sim_orders:{order.id}")
        for r in refunds:
            source_refs.append(f"sim_refunds:{r.id}")

        ledger = RefundBalanceLedger(
            payment_id=payment.id,
            order_id=payment.order_id or (order.id if order else order_id),
            currency=payment.currency,
            payment_status=payment.status,
            payment_verified=is_verified,
            eligible_amount=eligible_amount,
            settled_refunds=settled_refunds,
            pending_or_held_refunds=pending_or_held_refunds,
            refundable_remainder=refundable_remainder,
            refund_records_count=len(refunds),
            source_refs=source_refs,
        )
        return ledger, payment, order

    def evaluate_refund_request(
        self,
        *,
        requested_amount: int,
        payment_id: str | None = None,
        order_id: str | None = None,
        expected_merchant_id: str = "MERCHANT_MOCK_01",
        expected_currency: str = "VND",
        request_id: str | None = None,
        case_id: str | None = None,
        evidence_refs: list[str] | None = None,
        has_ambiguity: bool = False,
        persist_signals: bool = True,
        lock_rows: bool = False,
    ) -> RefundRiskEvaluation:
        """Deterministically evaluate a refund request against ledger state and policy rules."""
        ev_refs = list(evidence_refs or [])
        ledger, payment, order = self.compute_refundable_balance(
            payment_id=payment_id,
            order_id=order_id,
            lock_rows=lock_rows,
        )
        detected: list[DetectedRiskItem] = []

        # 1. Check request_id reuse across session refunds
        if request_id and request_id.strip():
            existing_req_ref = self._db.scalar(
                select(SimRefund).where(
                    SimRefund.session_id == self._session_id,
                    SimRefund.request_id == request_id.strip(),
                )
            )
            if existing_req_ref is not None:
                detected.append(
                    DetectedRiskItem(
                        category=RiskCategory.SUSPECTED_ABUSE.value,
                        level=RiskLevel.HIGH.value,
                        response=RiskResponse.HOLD_ACTION.value,
                        rule_code="DUPLICATE_REFUND_REQUEST_ID",
                        explanation_vi=(
                            f"Mã yêu cầu hoàn tiền '{request_id}' đã từng được sử dụng cho bản ghi "
                            f"hoàn tiền '{existing_req_ref.id}'. Tạm giữ hành động (HOLD_ACTION) "
                            "để chống hoàn tiền lặp (không quy kết khách hàng gian lận)."
                        ),
                        evidence_refs=ev_refs,
                        source_refs=[*ledger.source_refs, f"sim_refunds:{existing_req_ref.id}"],
                        details={
                            "request_id": request_id,
                            "existing_refund_id": existing_req_ref.id,
                            "existing_refund_status": existing_req_ref.status,
                        },
                    )
                )

        # 2. Check missing or unverified payment
        if payment is None or not ledger.payment_verified:
            detected.append(
                DetectedRiskItem(
                    category=RiskCategory.ERROR.value,
                    level=RiskLevel.HIGH.value,
                    response=RiskResponse.HOLD_ACTION.value,
                    rule_code="MISSING_OR_UNVERIFIED_PAYMENT",
                    explanation_vi=(
                        "Bản ghi thanh toán gốc không tồn tại hoặc chưa ở trạng thái SUCCESS "
                        f"(trạng thái hiện tại: {ledger.payment_status}). Không thể hoàn tiền."
                    ),
                    evidence_refs=ev_refs,
                    source_refs=ledger.source_refs,
                    details={
                        "payment_id": payment_id,
                        "order_id": order_id,
                        "payment_status": ledger.payment_status,
                        "result_code": payment.result_code if payment else None,
                    },
                )
            )
        else:
            # 3. Check order, merchant, currency, and source_mode consistency
            mismatch_reasons: list[str] = []
            if order_id and payment.order_id != order_id:
                mismatch_reasons.append(
                    f"payment.order_id ({payment.order_id}) != order_id ({order_id})"
                )
            if payment.merchant_id != expected_merchant_id:
                mismatch_reasons.append(
                    f"payment.merchant_id ({payment.merchant_id}) != {expected_merchant_id}"
                )
            if payment.currency != expected_currency:
                mismatch_reasons.append(
                    f"payment.currency ({payment.currency}) != {expected_currency}"
                )
            if order is not None and (
                order.source_mode != "MOCK" or order.currency != payment.currency
            ):
                mismatch_reasons.append("order source_mode or currency mismatch")

            if mismatch_reasons:
                detected.append(
                    DetectedRiskItem(
                        category=RiskCategory.ERROR.value,
                        level=RiskLevel.HIGH.value,
                        response=RiskResponse.HOLD_ACTION.value,
                        rule_code="ORDER_PAYMENT_MISMATCH",
                        explanation_vi=(
                            "Phát hiện sai lệch liên kết đơn hàng/thanh toán/merchant: "
                            f"{'; '.join(mismatch_reasons)}. Tạm giữ hoàn tiền (HOLD_ACTION)."
                        ),
                        evidence_refs=ev_refs,
                        source_refs=ledger.source_refs,
                        details={"mismatch_reasons": mismatch_reasons},
                    )
                )

            # 4. Check already fully refunded payment (QA T07)
            if ledger.settled_refunds >= ledger.eligible_amount and ledger.eligible_amount > 0:
                detected.append(
                    DetectedRiskItem(
                        category=RiskCategory.CONFIRMED_POLICY_VIOLATION.value,
                        level=RiskLevel.HIGH.value,
                        response=RiskResponse.HOLD_ACTION.value,
                        rule_code="ALREADY_REFUNDED_PAYMENT",
                        explanation_vi=(
                            f"Giao dịch {payment.id} ({ledger.eligible_amount:,} "
                            f"{ledger.currency}) đã được hoàn tiền toàn bộ "
                            f"({ledger.settled_refunds:,} {ledger.currency}). "
                            "Số dư khả dụng bằng 0; chặn hoàn tiền lặp (HOLD_ACTION)."
                        ),
                        evidence_refs=ev_refs,
                        source_refs=ledger.source_refs,
                        details={
                            "payment_id": payment.id,
                            "eligible_amount": ledger.eligible_amount,
                            "settled_refunds": ledger.settled_refunds,
                            "refundable_remainder": ledger.refundable_remainder,
                            "requested_amount": requested_amount,
                        },
                    )
                )
            # 5. Check in-flight PENDING or UNKNOWN refund (QA T08)
            elif ledger.pending_or_held_refunds > 0:
                detected.append(
                    DetectedRiskItem(
                        category=RiskCategory.ANOMALY.value,
                        level=RiskLevel.HIGH.value,
                        response=RiskResponse.HOLD_ACTION.value,
                        rule_code="IN_FLIGHT_PENDING_REFUND",
                        explanation_vi=(
                            f"Giao dịch {payment.id} đang có lệnh hoàn tiền chờ xử lý "
                            f"({ledger.pending_or_held_refunds:,} {ledger.currency} "
                            "PENDING/UNKNOWN). Tạm giữ tạo lệnh hoàn mới (HOLD_ACTION) "
                            "cho đến khi đối soát xong lệnh đang chờ."
                        ),
                        evidence_refs=ev_refs,
                        source_refs=ledger.source_refs,
                        details={
                            "payment_id": payment.id,
                            "eligible_amount": ledger.eligible_amount,
                            "settled_refunds": ledger.settled_refunds,
                            "pending_or_held_refunds": ledger.pending_or_held_refunds,
                            "refundable_remainder": ledger.refundable_remainder,
                            "requested_amount": requested_amount,
                        },
                    )
                )
            # 6. Check partial / cumulative over-refund (QA T09)
            elif requested_amount <= 0 or requested_amount > ledger.refundable_remainder:
                detected.append(
                    DetectedRiskItem(
                        category=RiskCategory.CONFIRMED_POLICY_VIOLATION.value,
                        level=RiskLevel.HIGH.value,
                        response=RiskResponse.HOLD_ACTION.value,
                        rule_code="CUMULATIVE_REFUND_LIMIT_EXCEEDED",
                        explanation_vi=(
                            f"Số tiền yêu cầu hoàn ({requested_amount:,} {ledger.currency}) "
                            f"vượt quá số dư khả dụng ({ledger.refundable_remainder:,} "
                            f"{ledger.currency} = gốc {ledger.eligible_amount:,} - đã hoàn "
                            f"{ledger.settled_refunds:,} - đang chờ "
                            f"{ledger.pending_or_held_refunds:,}). Chặn vượt mức (HOLD_ACTION)."
                        ),
                        evidence_refs=ev_refs,
                        source_refs=ledger.source_refs,
                        details={
                            "payment_id": payment.id,
                            "eligible_amount": ledger.eligible_amount,
                            "settled_refunds": ledger.settled_refunds,
                            "pending_or_held_refunds": ledger.pending_or_held_refunds,
                            "refundable_remainder": ledger.refundable_remainder,
                            "requested_amount": requested_amount,
                        },
                    )
                )

        # 7. Check ambiguity signal when no hard violation blocked it (QA T21)
        if not detected and has_ambiguity:
            detected.append(
                DetectedRiskItem(
                    category=RiskCategory.ANOMALY.value,
                    level=RiskLevel.MEDIUM.value,
                    response=RiskResponse.REVIEW.value,
                    rule_code="AMBIGUOUS_CLAIM_NARRATIVE",
                    explanation_vi=(
                        "Số dư hoàn tiền hợp lệ nhưng mô tả khiếu nại có tình tiết cần nhân viên "
                        "xác minh thêm (REVIEW). Tuyệt đối không coi là bằng chứng gian lận."
                    ),
                    evidence_refs=ev_refs,
                    source_refs=ledger.source_refs,
                    details={"requested_amount": requested_amount},
                )
            )

        if persist_signals and detected and case_id is not None:
            for item in detected:
                sig = self._persist_risk_signal_and_event(case_id=case_id, item=item)
                item.persisted_signal_id = sig.id

        has_hold = any(d.response == RiskResponse.HOLD_ACTION.value for d in detected)
        has_review = any(d.response == RiskResponse.REVIEW.value for d in detected)

        if has_hold:
            rec_response = RiskResponse.HOLD_ACTION.value
            allowed = False
            summary_vi = detected[0].explanation_vi
        elif has_review:
            rec_response = RiskResponse.REVIEW.value
            allowed = True
            summary_vi = detected[0].explanation_vi
        else:
            rec_response = RiskResponse.OBSERVE.value
            allowed = True
            summary_vi = (
                f"Yêu cầu hoàn tiền {requested_amount:,} {ledger.currency} nằm trong hạn mức "
                f"khả dụng ({ledger.refundable_remainder:,} {ledger.currency}); đủ điều kiện "
                "trình phê duyệt."
            )

        return RefundRiskEvaluation(
            allowed=allowed,
            recommended_response=rec_response,
            ledger=ledger,
            requested_amount=requested_amount,
            explanation_vi=summary_vi,
            signals=detected,
        )

    def record_governance_signal(
        self,
        *,
        case_id: str | None,
        category: RiskCategory,
        level: RiskLevel,
        response: RiskResponse,
        rule_code: str,
        explanation_vi: str,
        source_refs: list[str],
        evidence_refs: list[str] | None = None,
        details: dict[str, Any] | None = None,
    ) -> RiskSignal | None:
        """Record a governance/execution RiskSignal (e.g. expired approval or stale version)."""
        if case_id is None:
            return None
        item = DetectedRiskItem(
            category=category.value,
            level=level.value,
            response=response.value,
            rule_code=rule_code,
            explanation_vi=explanation_vi,
            evidence_refs=list(evidence_refs or []),
            source_refs=source_refs,
            details=details or {},
        )
        return self._persist_risk_signal_and_event(case_id=case_id, item=item)

    def _persist_risk_signal_and_event(
        self,
        *,
        case_id: str,
        item: DetectedRiskItem,
    ) -> RiskSignal:
        now = datetime.now(UTC)
        signal = RiskSignal(
            id=f"RSK-{uuid.uuid4().hex[:10].upper()}",
            case_id=case_id,
            session_id=self._session_id,
            category=item.category,
            level=item.level,
            evidence_refs=item.evidence_refs,
            response=item.response,
            details_json={
                "rule_code": item.rule_code,
                "explanation_vi": item.explanation_vi,
                "source_refs": item.source_refs,
                "non_accusatory_notice": (
                    "Tín hiệu kiểm soát rủi ro vận hành; không phải kết luận gian lận."
                ),
                **item.details,
            },
            created_at=now,
        )
        self._db.add(signal)

        evt = CaseEvent(
            id=f"EVT-{uuid.uuid4().hex[:12].upper()}",
            case_id=case_id,
            session_id=self._session_id,
            type="RISK_SIGNAL_DETECTED",
            payload_json={
                "risk_signal_id": signal.id,
                "category": item.category,
                "level": item.level,
                "response": item.response,
                "rule_code": item.rule_code,
                "explanation_vi": item.explanation_vi,
                "evidence_refs": item.evidence_refs,
                "source_refs": item.source_refs,
            },
            created_at=now,
        )
        self._db.add(evt)
        self._db.flush()
        return signal


def seed_risk_refund_cases(db: Session, session_id: str) -> dict[str, Any]:
    """Seed the two distinct Prompt 04 refund demonstration Cases in `session_id`:
    1. `valid_partial_refund`: Paid 500,000 VND order with 0 prior refunds and an eligible
       200,000 VND partial refund proposal ready for approval & execution in MOCK.
    2. `blocked_duplicate_refund`: Already-refunded 500,000 VND order (`ORD-REF-...`) where a
       repeat 500,000 VND refund complaint is deterministically blocked by RiskEngine with
       `CONFIRMED_POLICY_VIOLATION` and `HOLD_ACTION`.
    """
    now = datetime.now(UTC)
    suffix = uuid.uuid4().hex[:8].upper()

    # 1. Seed eligible paid order + payment for valid partial refund
    elig_order_id = f"ORD-ELIG-REF-{suffix}"
    elig_order = SimOrder(
        id=elig_order_id,
        session_id=session_id,
        customer_name="Phạm Hoàng Yến",
        customer_phone="0934567890",
        amount=500000,
        currency="VND",
        subtotal=500000,
        discount=0,
        tax=0,
        shipping=0,
        grand_total=500000,
        status=OrderStatus.PAID.value,
        payment_status=OrderPaymentStatus.PAID.value,
        fulfillment_status=FulfillmentStatus.UNFULFILLED.value,
        version=1,
        source_mode="MOCK",
        created_at=now,
        updated_at=now,
    )
    elig_payment = SimPayment(
        id=f"PAY-ELIG-REF-{suffix}",
        session_id=session_id,
        order_id=elig_order_id,
        merchant_id="MERCHANT_MOCK_01",
        provider="MOCK_MOMO",
        provider_ref=f"MOMO_TXN_ELIG_{suffix}",
        provider_order_id=f"MOMO_ORD_ELIG_{suffix}",
        request_id=f"REQ_ELIG_{suffix}",
        result_code=0,
        amount=500000,
        currency="VND",
        status=PaymentStatus.SUCCESS.value,
        verified_at=now,
        observed_at=now,
        verification_source="MOCK_MOMO_ADAPTER_VERIFIED",
        source_mode="MOCK",
        created_at=now,
    )
    db.add_all([elig_order, elig_payment])
    db.flush()

    valid_case = Case(
        id=f"CASE-REF-VALID-{suffix}",
        session_id=session_id,
        type="REFUND_REQUEST",
        goal=f"Yêu cầu hoàn tiền một phần (200.000 VND) cho đơn {elig_order_id}",
        state=CaseState.NEW.value,
        version=1,
        owner="guest",
        created_at=now,
        updated_at=now,
    )
    db.add(valid_case)
    db.flush()

    ev_claim_valid = Evidence(
        id=f"EVD-CLM-V-{suffix}",
        case_id=valid_case.id,
        session_id=session_id,
        kind=EvidenceKind.CLAIM.value,
        source="CUSTOMER_MESSAGE",
        source_ref="intake_text",
        payload_json={
            "text": (
                f"Đơn hàng {elig_order_id} giao thiếu 1 phụ kiện, "
                "tôi đề nghị hoàn lại một phần 200.000đ."
            )
        },
        observed_at=now,
    )
    ev_ord_valid = Evidence(
        id=f"EVD-ORD-V-{suffix}",
        case_id=valid_case.id,
        session_id=session_id,
        kind=EvidenceKind.OBSERVED_FACT.value,
        source="SIM_ORDERS_DB",
        source_ref=f"sim_orders:{elig_order.id}",
        payload_json={"order_id": elig_order.id, "amount": 500000, "status": "PAID"},
        confidence=1.0,
        observed_at=now,
    )
    ev_pay_valid = Evidence(
        id=f"EVD-PAY-V-{suffix}",
        case_id=valid_case.id,
        session_id=session_id,
        kind=EvidenceKind.OBSERVED_FACT.value,
        source="PROVIDER_LEDGER_MOCK",
        source_ref=f"sim_payments:{elig_payment.id}",
        payload_json={
            "payment_id": elig_payment.id,
            "order_id": elig_order.id,
            "amount": 500000,
            "status": "SUCCESS",
            "authoritative_success": True,
        },
        confidence=1.0,
        observed_at=now,
    )
    db.add_all([ev_claim_valid, ev_ord_valid, ev_pay_valid])
    db.flush()

    transition_case(
        db,
        valid_case,
        CaseState.INVESTIGATING,
        reason="Điều tra hạn mức hoàn tiền một phần hợp lệ",
    )
    valid_proposal = ActionProposal(
        id=f"PROP-REF-VALID-{suffix}",
        case_id=valid_case.id,
        session_id=session_id,
        action_type=ActionType.REFUND_EXECUTE.value,
        params_json={
            "payment_id": elig_payment.id,
            "order_id": elig_order.id,
            "merchant_id": elig_payment.merchant_id,
            "amount": 200000,
            "currency": "VND",
            "reason": (
                f"Hoàn tiền một phần 200.000 VND cho đơn {elig_order.id} "
                "(số dư khả dụng 500.000 VND)"
            ),
        },
        evidence_refs=[ev_ord_valid.id, ev_pay_valid.id],
        expected_version=valid_case.version,
        risk_level=RiskLevel.MEDIUM.value,
        status=ActionProposalStatus.PROPOSED.value,
        created_at=now,
    )
    db.add(valid_proposal)
    db.flush()
    transition_case(
        db,
        valid_case,
        CaseState.WAITING_APPROVAL,
        reason="Đủ số dư hoàn tiền khả dụng (500.000 VND); trình phê duyệt hoàn 200.000 VND",
        extra_payload={"proposal_id": valid_proposal.id},
    )

    # 2. Seed unsafe duplicate refund Case on the session's already-refunded order
    already_ref_order = db.scalar(
        select(SimOrder).where(
            SimOrder.session_id == session_id,
            SimOrder.id.like("ORD-REF-%"),
        )
    )
    if already_ref_order is None:
        already_ref_order = elig_order
    already_ref_pay = db.scalar(
        select(SimPayment).where(
            SimPayment.session_id == session_id,
            SimPayment.order_id == already_ref_order.id,
        )
    )
    assert already_ref_pay is not None  # noqa: S101

    blocked_case = Case(
        id=f"CASE-REF-BLOCK-{suffix}",
        session_id=session_id,
        type="REFUND_REQUEST",
        goal=f"Kiểm tra yêu cầu hoàn tiền lặp 500.000 VND cho đơn {already_ref_order.id}",
        state=CaseState.NEW.value,
        version=1,
        owner="guest",
        created_at=now,
        updated_at=now,
    )
    db.add(blocked_case)
    db.flush()

    ev_claim_blk = Evidence(
        id=f"EVD-CLM-B-{suffix}",
        case_id=blocked_case.id,
        session_id=session_id,
        kind=EvidenceKind.CLAIM.value,
        source="CUSTOMER_MESSAGE",
        source_ref="intake_text",
        payload_json={
            "text": f"Tôi yêu cầu hoàn tiền 500.000đ cho đơn hàng {already_ref_order.id}."
        },
        observed_at=now,
    )
    ev_ref_blk = Evidence(
        id=f"EVD-REF-B-{suffix}",
        case_id=blocked_case.id,
        session_id=session_id,
        kind=EvidenceKind.OBSERVED_FACT.value,
        source="SIM_REFUNDS_DB",
        source_ref=f"sim_refunds:{already_ref_pay.id}",
        payload_json={
            "payment_id": already_ref_pay.id,
            "order_id": already_ref_order.id,
            "settled_refunds": 500000,
            "refundable_remainder": 0,
        },
        confidence=1.0,
        observed_at=now,
    )
    db.add_all([ev_claim_blk, ev_ref_blk])
    db.flush()

    transition_case(
        db,
        blocked_case,
        CaseState.INVESTIGATING,
        reason="Điều tra rủi ro hoàn tiền trùng lặp",
    )
    risk_engine = RiskEngine(db, session_id)
    eval_blocked = risk_engine.evaluate_refund_request(
        requested_amount=500000,
        payment_id=already_ref_pay.id,
        order_id=already_ref_order.id,
        case_id=blocked_case.id,
        evidence_refs=[ev_ref_blk.id],
        persist_signals=True,
    )
    if can_transition(CaseState(blocked_case.state), CaseState.ESCALATED):
        transition_case(
            db,
            blocked_case,
            CaseState.ESCALATED,
            reason=eval_blocked.explanation_vi,
        )

    db.commit()
    return {
        "valid_partial_refund_case_id": valid_case.id,
        "valid_partial_refund_proposal_id": valid_proposal.id,
        "valid_partial_refund_order_id": elig_order.id,
        "valid_partial_refund_payment_id": elig_payment.id,
        "blocked_duplicate_refund_case_id": blocked_case.id,
        "blocked_duplicate_refund_order_id": already_ref_order.id,
        "blocked_risk_signal_ids": [
            s.persisted_signal_id for s in eval_blocked.signals if s.persisted_signal_id
        ],
    }
