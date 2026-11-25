"""Independent runtime configuration for FinShop LIVE Commerce API."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LOCAL_ENVS: frozenset[str] = frozenset({"local", "test", "dev", "development"})


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
    mock_provider_secret: SecretStr = SecretStr(
        "finshop-local-only-mock-provider-secret-2026-change-me"
    )
    enable_mock_payment_test_api: bool = True

    # Configurable initial test merchant account (hashed with scrypt in DB)
    test_merchant_username: str = "merchant_owner"
    test_merchant_password: SecretStr = SecretStr("FinShopLocal!2026")
    merchant_session_ttl_seconds: int = Field(default=28800, ge=60, le=604800)
    customer_session_ttl_seconds: int = Field(default=604800, ge=60, le=2592000)

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
    def _only_mock_supported(cls, value: str) -> str:
        if value.strip().upper() != "MOCK":
            raise ValueError("Only COMMERCE_SOURCE_MODE=MOCK is supported in Prompt 10.1")
        return "MOCK"

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
    def cors_origins(self) -> list[str]:
        return [o.strip().rstrip("/") for o in self.web_origin.split(",") if o.strip()]


@lru_cache
def get_settings() -> CommerceSettings:
    return CommerceSettings()
