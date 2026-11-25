"""Server-side Payment Provider Registry for FinShop LIVE.

Enforces explicit provider selection (`LOCAL_MOCK` vs `VNPAY_SANDBOX`), capability checks,
and strict isolation so VNPAY failures NEVER silently fall back to LOCAL_MOCK.
"""

from __future__ import annotations

from typing import Any

from app.core.config import get_settings
from app.core.errors import CommerceError, ErrorCode
from app.models.enums import PaymentProvider
from app.payments.adapter import PaymentAdapter
from app.payments.mock import MockPaymentAdapter
from app.payments.vnpay.adapter import VNPaySandboxAdapter
from app.payments.vnpay.client import VNPayHttpClient


def get_payment_adapter(
    provider: str,
    *,
    vnpay_http_client: VNPayHttpClient | None = None,
) -> PaymentAdapter:
    """Resolve the authoritative PaymentAdapter for a validated provider name."""
    norm = provider.strip().upper()
    if norm == PaymentProvider.LOCAL_MOCK.value:
        return MockPaymentAdapter()
    if norm == PaymentProvider.VNPAY_SANDBOX.value:
        return VNPaySandboxAdapter(http_client=vnpay_http_client)
    raise CommerceError(
        ErrorCode.BAD_REQUEST,
        f"Unsupported payment provider '{provider}'. Supported: LOCAL_MOCK, VNPAY_SANDBOX.",
        status_code=400,
    )


def list_payment_methods() -> list[dict[str, Any]]:
    """Return storefront payment method availability derived strictly from server config."""
    settings = get_settings()
    vnpay_ready = settings.is_vnpay_configured
    return [
        {
            "id": PaymentProvider.LOCAL_MOCK.value,
            "label_vi": "Thanh toán thử nghiệm nội bộ (LOCAL_MOCK — mô phỏng)",
            "available": True,
            "is_mock": True,
            "supports_redirect": False,
        },
        {
            "id": PaymentProvider.VNPAY_SANDBOX.value,
            "label_vi": (
                "VNPAY Sandbox (Môi trường thử nghiệm chính thức — chuyển hướng cổng VNPAY)"
                if vnpay_ready
                else "VNPAY Sandbox (Chưa cấu hình khóa Sandbox — tạm thời chưa khả dụng)"
            ),
            "available": vnpay_ready,
            "is_mock": False,
            "supports_redirect": vnpay_ready,
            "refund_enabled": vnpay_ready and settings.vnpay_refund_enabled,
        },
    ]
