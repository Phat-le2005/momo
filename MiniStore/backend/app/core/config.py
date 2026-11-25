"""Independent runtime configuration for FinShop LIVE Commerce API."""

from __future__ import annotations

from functools import lru_cache
from urllib.parse import urlparse

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LOCAL_ENVS: frozenset[str] = frozenset({"local", "test", "dev", "development"})
APPROVED_VNPAY_SANDBOX_HOSTS: frozenset[str] = frozenset({"sandbox.vnpayment.vn"})
TEST_ONLY_VNPAY_HOSTS: frozenset[str] = frozenset({"localhost", "127.0.0.1", "vnpay.test"})


class CommerceSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="COMMERCE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_env: str = "local"
    app_name: str = "finshop-commerce-api"
    database_url: str = "postgresql+psycopg://finshop:finshop@localhost:5434/finshop_commerce"
    db_connect_timeout_seconds: int = Field(default=3, ge=1, le=30)
    web_origin: str = "http://localhost:3100"
    source_mode: str = "MOCK"

    auth_secret: SecretStr = SecretStr("finshop-local-only-dev-hmac-secret-key-2026-change-me")
    service_api_key: SecretStr = SecretStr(
        "finshop-local-only-service-connector-key-2026-change-me"
    )
    service_write_api_key: SecretStr = SecretStr(
        "finshop-local-only-service-write-key-2026-change-me"
    )
    chaos_enabled: bool = False
    chaos_admin_key: SecretStr = SecretStr("finshop-local-only-chaos-admin-key-2026-change-me")
    mock_provider_secret: SecretStr = SecretStr(
        "finshop-local-only-mock-provider-secret-2026-change-me"
    )
    enable_mock_payment_test_api: bool = True

    # Configurable initial test merchant account (hashed with scrypt in DB)
    test_merchant_username: str = "merchant_owner"
    test_merchant_password: SecretStr = SecretStr("FinShopLocal!2026")
    merchant_session_ttl_seconds: int = Field(default=28800, ge=60, le=604800)
    customer_session_ttl_seconds: int = Field(default=604800, ge=60, le=2592000)

    # VNPAY Sandbox config (disabled by default; missing creds never block MOCK startup)
    vnpay_enabled: bool = False
    vnpay_tmn_code: str = ""
    vnpay_hash_secret: SecretStr = SecretStr("")
    vnpay_version: str = "2.1.0"
    vnpay_payment_url: str = "https://sandbox.vnpayment.vn/paymentv2/vpcpay.html"
    vnpay_api_url: str = "https://sandbox.vnpayment.vn/merchant_webapi/api/transaction"
    vnpay_return_url: str = "http://localhost:3100/payment/vnpay/return"
    vnpay_ipn_public_url: str = ""
    vnpay_refund_enabled: bool = False
    vnpay_timeout_seconds: float = Field(default=10.0, ge=1.0, le=60.0)
    vnpay_payment_expiry_minutes: int = Field(default=15, ge=5, le=120)
    vnpay_querydr_max_attempts: int = Field(default=5, ge=1, le=20)
    vnpay_querydr_min_age_seconds: int = Field(default=30, ge=0, le=3600)

    # Public HTTPS gateway configuration (explicitly disabled by default)
    public_gateway_enabled: bool = False
    public_base_url: str = ""

    # Optional FinResolve integration (Commerce must work cleanly when empty/offline)
    finresolve_api_base_url: str = ""
    finresolve_session_token: SecretStr = SecretStr("")
    finresolve_service_token: SecretStr = SecretStr("")
    finresolve_target_world_epoch: int | None = None

    reservation_ttl_seconds: int = Field(default=900, ge=5, le=86400)
    worker_poll_seconds: float = Field(default=1.5, gt=0.1, le=60.0)
    outbox_poll_seconds: float = Field(default=2.0, gt=0.1, le=60.0)
    outbox_lease_seconds: int = Field(default=30, ge=5, le=300)
    outbox_max_attempts: int = Field(default=10, ge=1, le=50)
    max_request_body_bytes: int = Field(default=1_048_576, ge=1024, le=10_485_760)
    rate_limit_checkout_per_minute: int = Field(default=120, ge=1, le=10_000)
    rate_limit_auth_per_minute: int = Field(default=60, ge=1, le=10_000)
    rate_limit_payment_per_minute: int = Field(default=120, ge=1, le=10_000)

    @field_validator("web_origin")
    @classmethod
    def _no_wildcard_origin(cls, value: str) -> str:
        if "*" in value:
            raise ValueError("COMMERCE_WEB_ORIGIN must list explicit origins; wildcard not allowed")
        return value

    @field_validator("source_mode")
    @classmethod
    def _only_mock_or_sandbox_supported(cls, value: str) -> str:
        norm = value.strip().upper()
        if norm not in {"MOCK", "SANDBOX"}:
            raise ValueError("Only COMMERCE_SOURCE_MODE=MOCK or SANDBOX is supported")
        return norm

    @model_validator(mode="after")
    def _validate_vnpay_sandbox_hosts(self) -> CommerceSettings:
        allowed_hosts = set(APPROVED_VNPAY_SANDBOX_HOSTS)
        if self.app_env.strip().lower() == "test":
            allowed_hosts |= TEST_ONLY_VNPAY_HOSTS

        for label, raw_url in (
            ("COMMERCE_VNPAY_PAYMENT_URL", self.vnpay_payment_url.strip()),
            ("COMMERCE_VNPAY_API_URL", self.vnpay_api_url.strip()),
        ):
            if not raw_url:
                continue
            parsed = urlparse(raw_url)
            host = (parsed.hostname or "").lower()
            if parsed.scheme not in {"https", "http"} or (
                parsed.scheme == "http" and self.app_env.strip().lower() != "test"
            ):
                raise ValueError(f"{label} must use HTTPS (production endpoints forbidden)")
            if host not in allowed_hosts:
                raise ValueError(
                    f"{label} host '{host}' is not an approved VNPAY Sandbox host "
                    f"(allowed: {sorted(APPROVED_VNPAY_SANDBOX_HOSTS)}). "
                    "Production VNPAY endpoints are strictly forbidden."
                )
        return self

    @model_validator(mode="after")
    def _validate_secrets(self) -> CommerceSettings:
        if self.app_env.strip().lower() not in LOCAL_ENVS:
            for name, secret in (
                ("COMMERCE_AUTH_SECRET", self.auth_secret.get_secret_value().strip()),
                ("COMMERCE_SERVICE_API_KEY", self.service_api_key.get_secret_value().strip()),
                (
                    "COMMERCE_MOCK_PROVIDER_SECRET",
                    self.mock_provider_secret.get_secret_value().strip(),
                ),
            ):
                if (
                    len(secret) < 32
                    or "local-only" in secret.lower()
                    or "change-me" in secret.lower()
                ):
                    raise ValueError(
                        f"{name} must be a strong >= 32-char secret in deployed environments"
                    )
            mpw = self.test_merchant_password.get_secret_value().strip()
            if len(mpw) < 12 or "finshoplocal" in mpw.lower():
                raise ValueError(
                    "COMMERCE_TEST_MERCHANT_PASSWORD must be overridden with a strong "
                    ">= 12-char password in deployed environments"
                )
        return self

    @property
    def is_local_env(self) -> bool:
        return self.app_env.strip().lower() in LOCAL_ENVS

    @property
    def is_vnpay_configured(self) -> bool:
        return bool(
            self.vnpay_enabled
            and self.vnpay_tmn_code.strip()
            and self.vnpay_hash_secret.get_secret_value().strip()
            and self.vnpay_return_url.strip()
        )

    @property
    def cors_origins(self) -> list[str]:
        origins = [o.strip().rstrip("/") for o in self.web_origin.split(",") if o.strip()]
        pub = self.public_base_url.strip().rstrip("/")
        if self.public_gateway_enabled and pub and pub not in origins:
            origins.append(pub)
        return origins


@lru_cache
def get_settings() -> CommerceSettings:
    return CommerceSettings()
