"""Runtime configuration loaded from environment variables (never hard-code secrets)."""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

INSECURE_DEFAULT_SECRETS: frozenset[str] = frozenset(
    {
        "",
        "finresolve-demo-manager-hmac-signing-key-local-only",
        "finresolve-dev-only-hmac-approval-secret-2026-change-me",
        "finresolve_docker_hmac_signing_key_v1_9f8e7d6c5b4a3f2e",
        "replace_with_strong_random_32_plus_char_hmac_secret_key",
        "replace-with-strong-random-32-plus-char-hmac-secret-key",
        "change-me",
        "change-me-local-only",
        "default",
        "secret",
        "password",
        "123456",
        "demo-secret",
    }
)
FORBIDDEN_REPO_EXAMPLE_SECRETS: frozenset[str] = frozenset(
    {
        "finresolve_docker_hmac_signing_key_v1_9f8e7d6c5b4a3f2e",
        "replace_with_strong_random_32_plus_char_hmac_secret_key",
        "replace-with-strong-random-32-plus-char-hmac-secret-key",
        "finresolve-dev-only-hmac-approval-secret-2026-change-me",
    }
)
LOCAL_DEV_ENVIRONMENTS: frozenset[str] = frozenset({"local", "test", "dev", "development"})


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

    demo_max_scenario_runs_per_session: int = Field(default=10, ge=1, le=200)
    demo_stale_run_timeout_seconds: int = Field(default=120, ge=1, le=3600)
    max_request_body_bytes: int = Field(default=1_048_576, ge=1024, le=10_485_760)
    trust_proxy_headers: bool = False
    rate_limit_sessions_per_minute: int = Field(default=60, ge=1, le=10_000)
    rate_limit_ai_calls_per_minute: int = Field(default=60, ge=1, le=10_000)

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

    @model_validator(mode="after")
    def _validate_demo_approval_secret(self) -> Settings:
        env_norm = self.app_env.strip().lower()
        secret_val = self.demo_approval_secret.get_secret_value().strip()
        secret_lower = secret_val.lower()

        # Always reject trivial weak placeholder values or known repository example/docker keys
        if (
            secret_lower in {"change-me", "default", "secret", "password", "123456", ""}
            or secret_val in FORBIDDEN_REPO_EXAMPLE_SECRETS
            or secret_lower in FORBIDDEN_REPO_EXAMPLE_SECRETS
            or "replace_with_" in secret_lower
            or "replace-with-" in secret_lower
        ):
            raise ValueError(
                "DEMO_APPROVAL_SECRET cannot be empty, a known repository example/default key, "
                "or an insecure placeholder value"
            )

        # In deployed environments (docker, production, staging, etc.), strictly require
        # an external strong random secret and reject all known defaults/examples.
        if env_norm not in LOCAL_DEV_ENVIRONMENTS:
            if (
                secret_val in INSECURE_DEFAULT_SECRETS
                or secret_lower in INSECURE_DEFAULT_SECRETS
                or "local-only" in secret_lower
                or "change-me" in secret_lower
                or "example" in secret_lower
                or "placeholder" in secret_lower
                or len(secret_val) < 32
            ):
                raise ValueError(
                    f"Deployed environment (APP_ENV='{self.app_env}') requires an explicit, "
                    "strong DEMO_APPROVAL_SECRET (>= 32 chars, not a repository example or default)"
                )
        return self

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
