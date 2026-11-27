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
import struct
from dataclasses import dataclass, field
from typing import Any

MAX_RECEIPT_BYTES = 5 * 1024 * 1024
MAX_IMAGE_DIMENSION = 8000
MAX_IMAGE_PIXELS = 25_000_000
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


def _parse_image_dimensions(content: bytes, norm_mime: str) -> tuple[int, int] | None:
    """Safely parse image width/height from headers without decoding full pixel buffers."""
    if norm_mime == "image/png":
        if len(content) < 24 or content[12:16] != b"IHDR":
            return None
        w, h = struct.unpack(">II", content[16:24])
        return int(w), int(h)
    if norm_mime == "image/jpeg":
        idx = 2
        n = len(content)
        while idx + 4 <= n:
            if content[idx] != 0xFF:
                idx += 1
                continue
            marker = content[idx + 1]
            if marker in (0xC0, 0xC1, 0xC2):
                if idx + 9 <= n:
                    h, w = struct.unpack(">HH", content[idx + 5 : idx + 9])
                    return int(w), int(h)
                return None
            if marker in (0xD8, 0xD9, 0x01) or (0xD0 <= marker <= 0xD7):
                idx += 2
                continue
            seg_len = struct.unpack(">H", content[idx + 2 : idx + 4])[0]
            if seg_len < 2:
                return None
            idx += 2 + seg_len
        return None
    if norm_mime == "image/webp":
        if len(content) >= 30 and content[12:16] == b"VP8X":
            w = 1 + int.from_bytes(content[24:27], "little")
            h = 1 + int.from_bytes(content[27:30], "little")
            return w, h
        if len(content) >= 30 and content[12:16] == b"VP8 ":
            w = struct.unpack("<H", content[26:28])[0] & 0x3FFF
            h = struct.unpack("<H", content[28:30])[0] & 0x3FFF
            return int(w), int(h)
        return None
    return None


def validate_receipt_upload(
    *,
    content: bytes,
    content_type: str,
) -> tuple[bool, str]:
    """Validate MIME type, size, magic bytes, and decompression-bomb dimension limits."""
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
    if norm_mime.startswith("image/"):
        dims = _parse_image_dimensions(content, norm_mime)
        if dims is not None:
            w, h = dims
            if (
                w <= 0
                or h <= 0
                or w > MAX_IMAGE_DIMENSION
                or h > MAX_IMAGE_DIMENSION
                or (w * h) > MAX_IMAGE_PIXELS
            ):
                return (
                    False,
                    f"DECOMPRESSION_BOMB_BLOCKED: image dimensions {w}x{h} exceed safety limits",
                )
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
    expected_merchant_id: str | None = None,
    ocr_available: bool = True,
    image_unreadable: bool = False,
) -> ReceiptClaimVerification:
    """Strictly compare customer receipt claim against authoritative Commerce records.

    Hard Safety Invariants (Prompt 14.1 Section 17):
    - Missing essential fields (transaction reference or amount) MUST NOT return CLAIM_SUPPORTED.
    - Mismatched transaction reference, order association, or merchant identity returns
      CLAIM_CONTRADICTED even when the amount matches!
    - Unavailable OCR/vision or unreadable images return MANUAL_REVIEW_REQUIRED without
      fabricating extracted fields.
    - NEVER confirms payment from a receipt image; NEVER accuses customers of fraud automatically.
    """
    extracted, injection_hit = extract_claim_fields_from_text(raw_claim_text or "")
    comparison: dict[str, Any] = {
        "authoritative_payment_present": authoritative_payment is not None,
        "authoritative_payment_status": (
            authoritative_payment.get("status") if authoritative_payment else None
        ),
        "authoritative_amount_vnd": (
            authoritative_payment.get("amount_vnd") if authoritative_payment else None
        ),
        "authoritative_order_id": (authoritative_order.get("id") if authoritative_order else None),
        "authoritative_order_status": (
            authoritative_order.get("status") if authoritative_order else None
        ),
        "authoritative_txn_ref": (
            authoritative_payment.get("provider_txn_ref")
            or authoritative_payment.get("payment_id")
            or authoritative_payment.get("id")
            if authoritative_payment
            else None
        ),
    }

    if injection_hit:
        return ReceiptClaimVerification(
            verdict="MANUAL_REVIEW_REQUIRED",
            evidence_kind="CLAIM",
            extracted_claim=extracted,
            authoritative_comparison=comparison,
            discrepancies=["Suspicious instruction-like text in customer claim"],
            prompt_injection_detected=True,
            rationale=(
                "Untrusted customer text contains instruction-like patterns; quarantined as data "
                "and routed to manual review without executing any action."
            ),
        )

    if not ocr_available or image_unreadable:
        reason = "OCR_OR_VISION_UNAVAILABLE" if not ocr_available else "UNREADABLE_IMAGE"
        return ReceiptClaimVerification(
            verdict="MANUAL_REVIEW_REQUIRED",
            evidence_kind="CLAIM",
            extracted_claim=extracted,
            authoritative_comparison=comparison,
            discrepancies=[f"{reason}: cannot reliably extract receipt fields from image"],
            prompt_injection_detected=False,
            rationale=(
                f"Receipt image could not be reliably extracted ({reason}); never fabricating "
                "extracted fields. Routed to manual review."
            ),
        )

    if authoritative_payment is None:
        return ReceiptClaimVerification(
            verdict="INSUFFICIENT_EVIDENCE",
            evidence_kind="CLAIM",
            extracted_claim=extracted,
            authoritative_comparison=comparison,
            discrepancies=["No authoritative payment record found for comparison"],
            prompt_injection_detected=False,
            rationale=(
                "Cannot verify customer claim without an authoritative payment record. "
                "Never confirm payment from an uploaded image; escalate for review."
            ),
        )

    discrepancies: list[str] = []
    missing_fields: list[str] = []

    claimed_ref = str(extracted.get("claimed_txn_ref") or "").strip()
    claimed_amt = extracted.get("claimed_amount_vnd")
    claimed_order = str(extracted.get("claimed_order_id") or "").strip()

    if not claimed_ref:
        missing_fields.append("Missing transaction reference in customer receipt claim")
    if claimed_amt is None:
        missing_fields.append("Missing claimed payment amount in customer receipt claim")

    auth_status = str(authoritative_payment.get("status") or "").upper()
    auth_amt = int(authoritative_payment.get("amount_vnd") or 0)
    auth_order_id = str(authoritative_payment.get("order_id") or "").strip()
    auth_merchant_id = str(authoritative_payment.get("merchant_id") or "").strip()
    auth_refs = {
        str(v).strip().upper()
        for v in (
            authoritative_payment.get("provider_txn_ref"),
            authoritative_payment.get("payment_id"),
            authoritative_payment.get("id"),
        )
        if v
    }

    # Missing authoritative identifiers MUST NEVER silently bypass validation!
    if not auth_refs:
        missing_fields.append("Authoritative payment record is missing transaction reference")
    if auth_amt <= 0:
        missing_fields.append("Authoritative payment record is missing valid amount")
    if not auth_order_id:
        missing_fields.append("Authoritative payment record is missing order association")
    if expected_merchant_id and not auth_merchant_id:
        missing_fields.append("Authoritative payment record is missing merchant identity")

    if auth_status != "SUCCEEDED":
        discrepancies.append(f"Authoritative payment status is '{auth_status}', not SUCCEEDED")
    if claimed_amt is not None and auth_amt > 0 and int(claimed_amt) != auth_amt:
        discrepancies.append(
            f"Claimed amount {int(claimed_amt):,}đ contradicts authoritative amount {auth_amt:,}đ"
        )
    if claimed_ref and auth_refs and claimed_ref.upper() not in auth_refs:
        discrepancies.append(
            f"Claimed transaction reference '{claimed_ref}' does not match authoritative "
            f"reference(s) {sorted(auth_refs)}"
        )
    if authoritative_order is not None:
        target_order_id = str(authoritative_order.get("id") or "").strip()
        if target_order_id and auth_order_id and auth_order_id != target_order_id:
            discrepancies.append(
                f"Authoritative payment belongs to order '{auth_order_id}', not '{target_order_id}'"
            )
        if claimed_order and target_order_id and claimed_order.upper() != target_order_id.upper():
            discrepancies.append(
                f"Claimed order '{claimed_order}' does not match affected order '{target_order_id}'"
            )
    if expected_merchant_id and auth_merchant_id and auth_merchant_id != expected_merchant_id:
        discrepancies.append(
            f"Authoritative payment merchant '{auth_merchant_id}' does not match "
            f"expected merchant '{expected_merchant_id}'"
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

    if missing_fields:
        return ReceiptClaimVerification(
            verdict="INSUFFICIENT_EVIDENCE",
            evidence_kind="CLAIM",
            extracted_claim=extracted,
            authoritative_comparison=comparison,
            discrepancies=missing_fields,
            prompt_injection_detected=False,
            rationale=(
                "Essential claim identifiers are missing; cannot mark CLAIM_SUPPORTED. "
                "Request clearer receipt details or route to manual review."
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
            "Customer claim aligns with authoritative SUCCEEDED payment record, "
            "matching transaction reference, order association, and amount."
        ),
    )
