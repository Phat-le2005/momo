"""Runtime configuration loaded from environment variables (never hard-code secrets)."""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class SourceMode(StrEnum):
    """Where business/payment data comes from. Only MOCK is implemented in the MVP."""

    MOCK = "MOCK"
    MOMO_SANDBOX = "MOMO_SANDBOX"


class ModelMode(StrEnum):
    """Effective reasoning mode reported to the UI.

    DEMO_RULES = deterministic fallback, must never be presented as generative AI.
    LLM_API = an external model provider is configured (adapter lands in Prompt 02).
    """

    DEMO_RULES = "DEMO_RULES"
    LLM_API = "LLM_API"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_env: str = "local"
    app_name: str = "finresolve-api"

    database_url: str = (
        "postgresql+psycopg://finresolve:change-me-local-only@localhost:5432/finresolve"
    )
    db_connect_timeout_seconds: int = Field(default=3, ge=1, le=30)

    # Comma-separated list of allowed browser origins. Never "*".
    web_origin: str = "http://localhost:3000"

    source_mode: SourceMode = SourceMode.MOCK
    demo_approval_secret: SecretStr = SecretStr(
        "finresolve-demo-manager-hmac-signing-key-local-only"
    )

    model_provider: str = ""
    model_name: str = ""
    model_api_key: SecretStr = SecretStr("")
    model_timeout_seconds: float = Field(default=20.0, gt=0, le=120)
    model_budget_tokens: int = Field(default=20000, ge=0)

    @field_validator("web_origin")
    @classmethod
    def _no_wildcard_origin(cls, value: str) -> str:
        if "*" in value:
            raise ValueError("WEB_ORIGIN must list explicit origins; wildcard is not allowed")
        return value

    @field_validator("source_mode")
    @classmethod
    def _only_mock_supported(cls, value: SourceMode) -> SourceMode:
        # MOMO_SANDBOX requires authorized credentials and an adapter that does not exist yet.
        if value is not SourceMode.MOCK:
            raise ValueError("Only SOURCE_MODE=MOCK is supported in this build")
        return value

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip().rstrip("/") for o in self.web_origin.split(",") if o.strip()]

    @property
    def model_mode(self) -> ModelMode:
        has_key = bool(self.model_api_key.get_secret_value().strip())
        if self.model_provider.strip() and self.model_name.strip() and has_key:
            return ModelMode.LLM_API
        return ModelMode.DEMO_RULES


@lru_cache
def get_settings() -> Settings:
    return Settings()
