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

    # Optional FinResolve integration (Commerce must work cleanly when empty/offline)
    finresolve_api_base_url: str = ""
    finresolve_session_token: SecretStr = SecretStr("")
    finresolve_service_token: SecretStr = SecretStr("")

    reservation_ttl_seconds: int = Field(default=900, ge=5, le=86400)
    worker_poll_seconds: float = Field(default=1.5, gt=0.1, le=60.0)
    max_request_body_bytes: int = Field(default=1_048_576, ge=1024, le=10_485_760)
    rate_limit_checkout_per_minute: int = Field(default=120, ge=1, le=10_000)
    rate_limit_auth_per_minute: int = Field(default=120, ge=1, le=10_000)

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
            raise ValueError("Only COMMERCE_SOURCE_MODE=MOCK is supported in Prompt 10")
        return "MOCK"

    @model_validator(mode="after")
    def _validate_secrets(self) -> CommerceSettings:
        if self.app_env.strip().lower() not in LOCAL_ENVS:
            for name, secret in (
                ("COMMERCE_AUTH_SECRET", self.auth_secret.get_secret_value().strip()),
                ("COMMERCE_SERVICE_API_KEY", self.service_api_key.get_secret_value().strip()),
            ):
                if (
                    len(secret) < 32
                    or "local-only" in secret.lower()
                    or "change-me" in secret.lower()
                ):
                    raise ValueError(
                        f"{name} must be a strong >= 32-char secret in deployed environments"
                    )
        return self

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip().rstrip("/") for o in self.web_origin.split(",") if o.strip()]


@lru_cache
def get_settings() -> CommerceSettings:
    return CommerceSettings()
