"""Vietnamese free-text complaint understanding and prompt-injection detector.

Hard Domain Invariants:
1. Customer text is strictly an UNTRUSTED CLAIM. Extracting `claimed_paid=True` or an
   `extracted_payment_ref` NEVER establishes payment truth.
2. Adversarial prompt-injection strings in Vietnamese or English are detected, logged,
   and stripped of any control authority.
"""

from __future__ import annotations

import re

from app.ai_core.schemas import IntentCategory, VietnameseCaseUnderstanding

# Order ID patterns: e.g. ORD-SYNC-1234AB, ORD-SYNC, ORD-REF, ORD-DELAY, O-001
_ORDER_ID_REGEX = re.compile(
    r"\b(ORD-[A-Z0-9]{2,}(?:-[A-Z0-9]{2,})*|O-[0-9]{2,}[A-Z0-9-]*)\b",
    re.IGNORECASE,
)

# Payment / Transaction reference patterns: e.g. MOMO_TXN_..., BANK_TXN_..., PAY-..., MOMO_ORD_...
_PAYMENT_REF_REGEX = re.compile(
    r"\b((?:MOMO|BANK)_(?:TXN|ORD)_[A-Z0-9_]+|PAY-[A-Z0-9-]+|TXN[_-][A-Z0-9_-]+)\b",
    re.IGNORECASE,
)

# SKU patterns: e.g. SKU-TSHIRT-..., SKU-THERMOS-...
_SKU_REGEX = re.compile(r"\b(SKU-[A-Z0-9-]+)\b", re.IGNORECASE)

# Vietnamese phone number pattern (10 digits starting with 03, 05, 07, 08, 09)
_PHONE_REGEX = re.compile(r"\b(0[35789][0-9]{8})\b")

# VND amount patterns:
# 1) "250.000đ", "250,000 VND", "1.500.000 vnđ"
_FORMATTED_VND_REGEX = re.compile(
    r"\b([0-9]{1,3}(?:[.,][0-9]{3})+)\s*(?:đ|d|vnd|vnđ|đồng)?\b",
    re.IGNORECASE,
)
# 2) "250000đ", "500000 VND", "180000 vnđ"
_PLAIN_VND_REGEX = re.compile(
    r"\b([1-9][0-9]{4,8})\s*(?:đ|d|vnd|vnđ|đồng)\b",
    re.IGNORECASE,
)
# 3) "250k", "500 nghìn", "500 ngàn"
_SHORT_K_VND_REGEX = re.compile(
    r"\b([1-9][0-9]{1,4})\s*(?:k|nghìn|ngàn)\b",
    re.IGNORECASE,
)

# Prompt injection / privilege escalation signatures (Vietnamese & English)
_INJECTION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "ignore_instructions",
        re.compile(
            r"(?:ignore|disregard|forget)\s+(?:all\s+)?(?:previous|prior|above|system)\s+"
            r"(?:instructions|rules|prompts|policies)",
            re.IGNORECASE,
        ),
    ),
    (
        "vi_ignore_instructions",
        re.compile(
            r"(?:bỏ\s+qua|phớt\s+lờ|xóa\s+bỏ)\s+(?:tất\s+cả\s+|mọi\s+)?"
            r"(?:hướng\s+dẫn|quy\s+tắc|chính\s+sách|lệnh\s+hệ\s+thống)",
            re.IGNORECASE,
        ),
    ),
    (
        "privilege_flag_override",
        re.compile(
            r"\b(?:payment_verified|is_trusted|adapter_verified|override_policy|"
            r"bypass_approval|auto_execute|is_authorized)\s*[:=]\s*(?:true|1)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "system_role_spoof",
        re.compile(
            r"(?:\[system\]|<system>|system\s+prompt\s*:|you\s+are\s+now\s+admin|"
            r"bạn\s+là\s+admin|cấp\s+quyền\s+cao\s+nhất)",
            re.IGNORECASE,
        ),
    ),
    (
        "direct_write_command_injection",
        re.compile(
            r"\b(?:execute_refund_proposal|execute_approved_action|drop\s+table|"
            r"update\s+sim_orders|update\s+cases\s+set)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "embedded_external_url",
        re.compile(r"(?:https?|ftp|file)://[^\s]+", re.IGNORECASE),
    ),
]

_REFUND_KEYWORDS = (
    "hoàn tiền",
    "hoan tien",
    "refund",
    "trả lại tiền",
    "tra lai tien",
    "hoàn lại",
    "hủy đơn hoàn",
    "chưa nhận được tiền hoàn",
)

_PAYMENT_KEYWORDS = (
    "đã thanh toán",
    "da thanh toan",
    "trừ tiền",
    "tru tien",
    "chuyển khoản",
    "chuyen khoan",
    "đã trả tiền",
    "chưa xác nhận",
    "chua xac nhan",
    "vẫn đang chờ",
    "van dang cho",
    "momo",
    "thanh toán thành công",
)

_INVENTORY_KEYWORDS = (
    "tồn kho",
    "ton kho",
    "hết hàng",
    "het hang",
    "chưa giao hàng",
    "kho hàng",
    "đặt giữ hàng",
)


def _extract_amounts_vnd(text: str) -> list[int]:
    amounts: list[int] = []
    seen: set[int] = set()

    for match in _FORMATTED_VND_REGEX.finditer(text):
        raw = match.group(1).replace(".", "").replace(",", "")
        if raw.isdigit():
            val = int(raw)
            if 1_000 <= val <= 10_000_000_000 and val not in seen:
                seen.add(val)
                amounts.append(val)

    for match in _PLAIN_VND_REGEX.finditer(text):
        raw = match.group(1)
        if raw.isdigit():
            val = int(raw)
            if 1_000 <= val <= 10_000_000_000 and val not in seen:
                seen.add(val)
                amounts.append(val)

    for match in _SHORT_K_VND_REGEX.finditer(text):
        raw = match.group(1)
        if raw.isdigit():
            val = int(raw) * 1000
            if 1_000 <= val <= 10_000_000_000 and val not in seen:
                seen.add(val)
                amounts.append(val)

    return amounts


def _dedupe_upper(items: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        up = item.strip().upper()
        if up and up not in seen:
            seen.add(up)
            result.append(up)
    return result


def analyze_vietnamese_complaint(
    text: str,
    case_type: str | None = None,
) -> VietnameseCaseUnderstanding:
    """Parse Vietnamese free-text intake into structured investigation signals."""
    clean_text = text.strip()
    lower_text = clean_text.lower()

    order_ids = _dedupe_upper([m.group(1) for m in _ORDER_ID_REGEX.finditer(clean_text)])
    payment_refs = _dedupe_upper([m.group(1) for m in _PAYMENT_REF_REGEX.finditer(clean_text)])
    skus = _dedupe_upper([m.group(1) for m in _SKU_REGEX.finditer(clean_text)])
    phones = _dedupe_upper([m.group(1) for m in _PHONE_REGEX.finditer(clean_text)])
    amounts = _extract_amounts_vnd(clean_text)

    # Detect prompt injection attempts
    detected_patterns: list[str] = []
    for label, pattern in _INJECTION_PATTERNS:
        if pattern.search(clean_text):
            detected_patterns.append(label)

    has_refund_kw = any(kw in lower_text for kw in _REFUND_KEYWORDS)
    has_payment_kw = any(kw in lower_text for kw in _PAYMENT_KEYWORDS)
    has_inventory_kw = any(kw in lower_text for kw in _INVENTORY_KEYWORDS)

    # Determine primary intent
    normalized_type = (case_type or "").strip().upper()
    if has_refund_kw or normalized_type in ("REFUND_REQUEST", "REFUND_DISPUTE", "REFUND_RISK"):
        intent = IntentCategory.REFUND_DISPUTE
    elif has_payment_kw or normalized_type in ("PAYMENT_DISPUTE", "ORDER_SYNC", "PAYMENT_SYNC"):
        intent = IntentCategory.PAYMENT_SYNC
    elif has_inventory_kw or normalized_type in ("INVENTORY_DISPUTE", "FULFILLMENT"):
        intent = IntentCategory.INVENTORY_OR_FULFILLMENT
    else:
        intent = IntentCategory.GENERAL_EXCEPTION

    claimed_paid = any(
        kw in lower_text
        for kw in (
            "đã thanh toán",
            "da thanh toan",
            "trừ tiền",
            "tru tien",
            "đã chuyển khoản",
            "đã trả tiền",
            "thanh toán thành công",
        )
    )
    claimed_refund = has_refund_kw

    parts: list[str] = [f"Phân loại khiếu nại: {intent.value} (Nguồn: CLAIM chưa xác thực)."]
    if order_ids:
        parts.append(f"Mã đơn đề cập: {', '.join(order_ids)}.")
    if payment_refs:
        parts.append(f"Mã giao dịch đề cập: {', '.join(payment_refs)}.")
    if amounts:
        formatted_amts = ", ".join(f"{a:,} VND" for a in amounts)
        parts.append(f"Số tiền đề cập: {formatted_amts}.")
    if detected_patterns:
        parts.append(
            f"CẢNH BÁO: Phát hiện dấu hiệu prompt injection ({', '.join(detected_patterns)}); "
            "đã cô lập nội dung như dữ liệu thụ động."
        )

    return VietnameseCaseUnderstanding(
        intent=intent,
        extracted_order_ids=order_ids,
        extracted_payment_refs=payment_refs,
        extracted_amounts_vnd=amounts,
        extracted_phones=phones,
        extracted_skus=skus,
        claimed_paid=claimed_paid,
        claimed_refund_requested=claimed_refund,
        prompt_injection_detected=bool(detected_patterns),
        prompt_injection_patterns=detected_patterns,
        summary_vi=" ".join(parts),
    )
