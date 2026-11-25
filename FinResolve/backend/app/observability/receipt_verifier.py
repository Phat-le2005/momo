"""Safe Multimodal Payment Claim & Receipt Evidence Verification (Prompt 14 Section 17).

Safety Invariants:
- Validates MIME type, file size (<= 5 MB), and image magic bytes; NEVER executes content.
- Treats ALL extracted receipt fields strictly as `CLAIM` (never payment proof!).
- Independently compares claimed fields against authoritative Commerce payment & order
  records to emit one of: `CLAIM_SUPPORTED`, `CLAIM_CONTRADICTED`, `INSUFFICIENT_EVIDENCE`,
  or `MANUAL_REVIEW_REQUIRED`.
- NEVER confirms a payment from an uploaded image and NEVER accuses a customer of fraud
  merely because evidence cannot be verified.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

MAX_RECEIPT_BYTES = 5 * 1024 * 1024
ALLOWED_MIME_TYPES: frozenset[str] = frozenset(
    {"image/png", "image/jpeg", "image/webp", "text/plain"}
)
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"
WEBP_MAGIC = b"RIFF"


@dataclass(frozen=True)
class ReceiptClaimVerification:
    verdict: (
        str  # CLAIM_SUPPORTED, CLAIM_CONTRADICTED, INSUFFICIENT_EVIDENCE, MANUAL_REVIEW_REQUIRED
    )
    evidence_kind: str  # Always "CLAIM" for extracted receipt fields
    extracted_claim: dict[str, Any]
    authoritative_comparison: dict[str, Any]
    discrepancies: list[str] = field(default_factory=list)
    prompt_injection_detected: bool = False
    rationale: str = ""


def validate_receipt_upload(
    *,
    content: bytes,
    content_type: str,
) -> tuple[bool, str]:
    """Validate uploaded receipt MIME type, size, and safe file header magic bytes."""
    norm_mime = content_type.strip().lower().split(";")[0]
    if norm_mime not in ALLOWED_MIME_TYPES:
        return False, f"UNSUPPORTED_MEDIA_TYPE: '{norm_mime}' is not allowed"
    if not content:
        return False, "EMPTY_FILE: uploaded receipt is empty"
    if len(content) > MAX_RECEIPT_BYTES:
        return False, f"FILE_TOO_LARGE: exceeds {MAX_RECEIPT_BYTES} bytes limit"
    if norm_mime == "image/png" and not content.startswith(PNG_MAGIC):
        return False, "INVALID_IMAGE_HEADER: file does not match PNG signature"
    if norm_mime == "image/jpeg" and not content.startswith(JPEG_MAGIC):
        return False, "INVALID_IMAGE_HEADER: file does not match JPEG signature"
    if norm_mime == "image/webp" and not (
        content.startswith(WEBP_MAGIC) and b"WEBP" in content[:16]
    ):
        return False, "INVALID_IMAGE_HEADER: file does not match WEBP signature"
    return True, "OK"


def extract_claim_fields_from_text(raw_text: str) -> tuple[dict[str, Any], bool]:
    """Extract structured payment claim fields from OCR/text while flagging prompt injection."""
    injection_patterns = (
        r"ignore\s+previous\s+instructions",
        r"system\s+override",
        r"approve\s+refund\s+immediately",
        r"mark\s+paid\s+immediately",
    )
    injection_hit = any(re.search(p, raw_text, re.IGNORECASE) for p in injection_patterns)

    ref_m = re.search(r"(?:ref|txn|mã giao dịch|biên lai)[:\s#]*([A-Z0-9_-]{5,40})", raw_text, re.I)
    ord_m = re.search(r"(ORD-[A-Z0-9_-]{4,32})", raw_text, re.I)
    amt_m = re.search(r"(\d[\d.,]{3,12})\s*(?:vnd|đ|d\b)", raw_text, re.I)

    claimed_amount: int | None = None
    if amt_m:
        digits = re.sub(r"[^\d]", "", amt_m.group(1))
        if digits.isdigit():
            claimed_amount = int(digits)

    return {
        "claimed_txn_ref": ref_m.group(1) if ref_m else None,
        "claimed_order_id": ord_m.group(1) if ord_m else None,
        "claimed_amount_vnd": claimed_amount,
        "is_synthetic_test_fixture": "MOCK" in raw_text.upper() or "TEST" in raw_text.upper(),
    }, injection_hit


def verify_payment_claim_against_records(
    *,
    raw_claim_text: str,
    authoritative_payment: dict[str, Any] | None,
    authoritative_order: dict[str, Any] | None,
) -> ReceiptClaimVerification:
    """Compare customer receipt claim against authoritative Commerce payment & order records."""
    extracted, injection_hit = extract_claim_fields_from_text(raw_claim_text)
    comparison: dict[str, Any] = {
        "authoritative_payment_present": authoritative_payment is not None,
        "authoritative_payment_status": (
            authoritative_payment.get("status") if authoritative_payment else None
        ),
        "authoritative_amount_vnd": (
            authoritative_payment.get("amount_vnd") if authoritative_payment else None
        ),
        "authoritative_order_status": (
            authoritative_order.get("status") if authoritative_order else None
        ),
    }

    if authoritative_payment is None:
        return ReceiptClaimVerification(
            verdict="INSUFFICIENT_EVIDENCE",
            evidence_kind="CLAIM",
            extracted_claim=extracted,
            authoritative_comparison=comparison,
            discrepancies=["No authoritative payment record found for comparison"],
            prompt_injection_detected=injection_hit,
            rationale=(
                "Cannot verify customer claim without an authoritative payment record. "
                "Never confirm payment from an uploaded image; escalate for review."
            ),
        )

    discrepancies: list[str] = []
    auth_status = str(authoritative_payment.get("status") or "").upper()
    auth_amt = int(authoritative_payment.get("amount_vnd") or 0)
    claimed_amt = extracted.get("claimed_amount_vnd")

    if auth_status != "SUCCEEDED":
        discrepancies.append(f"Authoritative payment status is '{auth_status}', not SUCCEEDED")
    if claimed_amt is not None and auth_amt > 0 and claimed_amt != auth_amt:
        discrepancies.append(
            f"Claimed amount {claimed_amt:,}đ contradicts authoritative amount {auth_amt:,}đ"
        )

    if injection_hit:
        return ReceiptClaimVerification(
            verdict="MANUAL_REVIEW_REQUIRED",
            evidence_kind="CLAIM",
            extracted_claim=extracted,
            authoritative_comparison=comparison,
            discrepancies=[*discrepancies, "Suspicious instruction-like text in customer claim"],
            prompt_injection_detected=True,
            rationale=(
                "Untrusted customer text contains instruction-like patterns; quarantined as data "
                "and routed to manual review without executing any action."
            ),
        )

    if discrepancies:
        return ReceiptClaimVerification(
            verdict="CLAIM_CONTRADICTED",
            evidence_kind="CLAIM",
            extracted_claim=extracted,
            authoritative_comparison=comparison,
            discrepancies=discrepancies,
            prompt_injection_detected=False,
            rationale=(
                "Customer payment claim is contradicted by authoritative records "
                "(requires human review; do not label customer as fraudulent automatically)."
            ),
        )

    return ReceiptClaimVerification(
        verdict="CLAIM_SUPPORTED",
        evidence_kind="CLAIM",
        extracted_claim=extracted,
        authoritative_comparison=comparison,
        discrepancies=[],
        prompt_injection_detected=False,
        rationale=(
            "Customer claim aligns with authoritative SUCCEEDED payment record and matching amount."
        ),
    )
