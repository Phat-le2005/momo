"""Official VNPAY 2.1.0 response code & transaction status mappings.

Distinguishes:
- API call success (`vnp_ResponseCode == "00"` on QueryDr/Refund) vs actual payment/refund status
  (`vnp_TransactionStatus`).
- Ambiguous, reversed (`04`), fraud-suspected (`07`), or unrecognized statuses map to
  `UNKNOWN` + `reconciliation_required=True` rather than fabricating success or failure.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models.enums import PaymentStatus, RefundTransactionStatus


@dataclass(frozen=True)
class VNPayStatusMapping:
    payment_status: PaymentStatus
    reconciliation_required: bool
    reason: str | None = None


@dataclass(frozen=True)
class VNPayRefundMapping:
    refund_status: RefundTransactionStatus
    reconciliation_required: bool
    reason: str | None = None


# Official VNPAY IPN acknowledgment codes returned by Merchant to VNPAY
IPN_RSP_SUCCESS = ("00", "Confirm Success")
IPN_RSP_ORDER_NOT_FOUND = ("01", "Order not found")
IPN_RSP_ALREADY_CONFIRMED = ("02", "Order already confirmed")
IPN_RSP_INVALID_AMOUNT = ("04", "Invalid amount")
IPN_RSP_INVALID_SIGNATURE = ("97", "Invalid Checksum")
IPN_RSP_UNKNOWN_ERROR = ("99", "Unknown error")


def map_vnpay_ipn_status(
    *,
    response_code: str,
    transaction_status: str | None,
) -> VNPayStatusMapping:
    """Map VNPAY IPN `(vnp_ResponseCode, vnp_TransactionStatus)` to internal `PaymentStatus`.

    P1.3 Hard Invariant:
    - Successful payment STRICTLY requires BOTH `vnp_ResponseCode == "00"` AND
      `vnp_TransactionStatus == "00"`.
    - Missing/empty `vnp_TransactionStatus` NEVER confirms an order; it maps to `UNKNOWN` with
      `reconciliation_required=True`.
    """
    rc = (response_code or "").strip()
    ts = (transaction_status or "").strip()

    if not rc or not ts:
        return VNPayStatusMapping(
            PaymentStatus.UNKNOWN,
            True,
            f"Incomplete VNPAY IPN status (ResponseCode='{rc}', TransactionStatus='{ts}')",
        )

    if rc == "00" and ts == "00":
        return VNPayStatusMapping(PaymentStatus.SUCCEEDED, False, None)
    if ts == "01":
        return VNPayStatusMapping(PaymentStatus.PENDING, False, "VNPAY transaction incomplete (01)")
    if ts == "04":
        return VNPayStatusMapping(
            PaymentStatus.UNKNOWN,
            True,
            "VNPAY transaction reversed (04): customer debited at bank, reconciliation required",
        )
    if ts == "07":
        return VNPayStatusMapping(
            PaymentStatus.UNKNOWN,
            True,
            "VNPAY suspected fraud (07): manual review and reconciliation required",
        )
    if rc == "00" and ts != "00":
        return VNPayStatusMapping(
            PaymentStatus.UNKNOWN,
            True,
            f"Conflicting VNPAY codes: ResponseCode=00 but TransactionStatus={ts}",
        )
    if rc == "24" or ts == "02" or rc in {"09", "10", "11", "12", "13", "51", "65", "75", "79"}:
        return VNPayStatusMapping(
            PaymentStatus.FAILED,
            False,
            f"VNPAY payment failed (ResponseCode={rc}, TransactionStatus={ts})",
        )
    return VNPayStatusMapping(
        PaymentStatus.UNKNOWN,
        True,
        f"Unrecognized VNPAY status (ResponseCode={rc}, TransactionStatus={ts})",
    )


def map_vnpay_querydr_status(
    *,
    response_code: str,
    transaction_status: str | None,
) -> VNPayStatusMapping:
    """Map VNPAY QueryDr response to internal `PaymentStatus`.

    IMPORTANT: `vnp_ResponseCode == "00"` indicates the QueryDr API call itself succeeded.
    Actual payment state comes from `vnp_TransactionStatus`!
    """
    rc = (response_code or "").strip()
    ts = (transaction_status or "").strip()

    if rc == "91":
        # Transaction not found at VNPAY yet (customer may still be on gateway or abandoned)
        return VNPayStatusMapping(
            PaymentStatus.PENDING, False, "VNPAY QueryDr 91: transaction not found at provider yet"
        )
    if rc != "00":
        return VNPayStatusMapping(
            PaymentStatus.UNKNOWN,
            True,
            f"VNPAY QueryDr API non-zero response_code={rc}",
        )

    if ts == "00":
        return VNPayStatusMapping(PaymentStatus.SUCCEEDED, False, None)
    if ts == "01":
        return VNPayStatusMapping(PaymentStatus.PENDING, False, "VNPAY QueryDr: pending (01)")
    if ts == "02":
        return VNPayStatusMapping(PaymentStatus.FAILED, False, "VNPAY QueryDr: failed (02)")
    if ts == "04":
        return VNPayStatusMapping(
            PaymentStatus.UNKNOWN,
            True,
            "VNPAY QueryDr: reversed transaction (04) — manual reconciliation required",
        )
    if ts in {"05", "06"}:
        return VNPayStatusMapping(
            PaymentStatus.UNKNOWN,
            True,
            f"VNPAY QueryDr: refund in progress at provider ({ts}) — reconciliation required",
        )
    if ts == "07":
        return VNPayStatusMapping(
            PaymentStatus.UNKNOWN,
            True,
            "VNPAY QueryDr: suspected fraud (07) — manual reconciliation required",
        )
    if ts == "09":
        return VNPayStatusMapping(
            PaymentStatus.UNKNOWN,
            True,
            "VNPAY QueryDr: refund rejected (09) — manual reconciliation required",
        )
    return VNPayStatusMapping(
        PaymentStatus.UNKNOWN,
        True,
        f"VNPAY QueryDr: ambiguous TransactionStatus='{ts}'",
    )


def map_vnpay_refund_response(
    *,
    response_code: str,
    transaction_status: str | None,
) -> VNPayRefundMapping:
    """Map VNPAY Refund API response.

    Never fabricates `SUCCEEDED` when VNPAY reports processing (`05`/`06`) or omits final status.
    """
    rc = (response_code or "").strip()
    ts = (transaction_status or "").strip()

    if rc != "00":
        if rc in {"91", "94", "95", "97", "99"}:
            return VNPayRefundMapping(
                RefundTransactionStatus.FAILED
                if rc in {"91", "95", "97"}
                else RefundTransactionStatus.UNKNOWN,
                reconciliation_required=(rc in {"94", "99"}),
                reason=f"VNPAY Refund API response_code={rc}",
            )
        return VNPayRefundMapping(
            RefundTransactionStatus.UNKNOWN,
            True,
            f"VNPAY Refund API uncertain response_code={rc}",
        )

    # ResponseCode == "00": VNPAY accepted the refund request
    if ts == "00":
        return VNPayRefundMapping(RefundTransactionStatus.SUCCEEDED, False, None)
    if ts in {"05", "06", ""}:
        return VNPayRefundMapping(
            RefundTransactionStatus.PENDING,
            False,
            f"VNPAY accepted refund (TransactionStatus={ts or 'PENDING'}); awaiting settlement",
        )
    if ts == "09":
        return VNPayRefundMapping(
            RefundTransactionStatus.FAILED,
            True,
            "VNPAY refund rejected (TransactionStatus=09)",
        )
    return VNPayRefundMapping(
        RefundTransactionStatus.UNKNOWN,
        True,
        f"VNPAY refund returned ambiguous TransactionStatus={ts}",
    )
