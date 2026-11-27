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
        "finresolve-local-only-command-auth-hmac-secret-2026-change-me",
        "finshop-local-only-service-write-key-2026-change-me",
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
    model_fallback_name: str = ""
    model_api_key: SecretStr = SecretStr("")
    model_timeout_seconds: float = Field(default=20.0, gt=0, le=120)
    model_max_retries: int = Field(default=2, ge=0, le=5)
    model_budget_tokens: int = Field(default=20000, ge=0)

    demo_max_scenario_runs_per_session: int = Field(default=10, ge=1, le=200)
    demo_stale_run_timeout_seconds: int = Field(default=120, ge=1, le=3600)
    max_request_body_bytes: int = Field(default=1_048_576, ge=1024, le=10_485_760)
    trust_proxy_headers: bool = False
    trusted_proxy_ips: str = ""
    rate_limit_sessions_per_minute: int = Field(default=60, ge=1, le=10_000)
    rate_limit_ai_calls_per_minute: int = Field(default=60, ge=1, le=10_000)
    max_concurrent_llm_calls: int = Field(default=4, ge=1, le=100)
    enable_embedded_supervisor_worker: bool = False
    worker_heartbeat_ttl_seconds: int = Field(default=15, ge=3, le=300)
    supervisor_job_lease_seconds: int = Field(default=30, ge=3, le=600)
    supervisor_heartbeat_interval_seconds: float = Field(default=5.0, gt=0.1, le=120.0)

    # Prompt 12 — Autonomous Continuous Commerce Monitor
    continuous_monitor_enabled: bool = False
    commerce_service_base_url: str = ""
    commerce_service_key: SecretStr = SecretStr("")
    commerce_merchant_id: str = "MERCHANT_FINSHOP_01"
    commerce_session_id: str = "CSESS_FINSHOP_DEFAULT_01"
    commerce_world_epoch: int = Field(default=1, ge=1)
    monitor_poll_interval_seconds: float = Field(default=5.0, ge=0.5, le=300.0)
    monitor_reconcile_interval_seconds: float = Field(default=15.0, ge=1.0, le=600.0)
    monitor_batch_size: int = Field(default=50, ge=1, le=200)
    monitor_lease_seconds: int = Field(default=30, ge=5, le=300)
    monitor_payment_sync_grace_seconds: int = Field(default=30, ge=0, le=3600)
    monitor_order_stall_seconds: int = Field(default=1800, ge=5, le=86400 * 7)
    monitor_max_concurrent_investigations: int = Field(default=5, ge=1, le=50)
    monitor_outbox_lag_threshold: int = Field(default=10, ge=1, le=10000)
    monitor_unsynced_backlog_threshold: int = Field(default=5, ge=1, le=10000)
    commerce_connector_timeout_seconds: float = Field(default=5.0, gt=0.1, le=60.0)
    commerce_connector_id: str = "default_ministore"
    commerce_allowed_hosts: str = "localhost,127.0.0.1,commerce-api"
    commerce_public_storefront_url: str = "http://localhost:3100"
    experience_operator_token: SecretStr = SecretStr("")
    sandbox_dedicated_mode: bool = False
    autonomous_recovery_enabled: bool = True
    commerce_write_key: SecretStr = SecretStr("")
    command_auth_signing_secret: SecretStr = SecretStr("")
    command_auth_key_id: str = "finresolve-policy-hmac-v1"
    command_auth_ttl_seconds: int = Field(default=120, ge=10, le=900)
    judge_admin_secret: SecretStr = SecretStr(
        "finresolve-judge-lab-local-only-secret-2026-change-me"
    )
    monitor_max_pages_per_tick: int = Field(default=10, ge=1, le=100)
    monitor_max_scan_seconds: float = Field(default=25.0, ge=1.0, le=300.0)

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

    @model_validator(mode="after")
    def _validate_recovery_and_signing_secrets(self) -> Settings:
        env_norm = self.app_env.strip().lower()
        sign_sec = self.command_auth_signing_secret.get_secret_value().strip()
        write_key = self.commerce_write_key.get_secret_value().strip()
        read_key = self.commerce_service_key.get_secret_value().strip()

        # Always reject known rotated/example signing or write secrets in EVERY environment
        for name, val in (
            ("COMMAND_AUTH_SIGNING_SECRET", sign_sec),
            ("COMMERCE_WRITE_KEY", write_key),
        ):
            if val and (
                val in FORBIDDEN_REPO_EXAMPLE_SECRETS
                or val.lower() in FORBIDDEN_REPO_EXAMPLE_SECRETS
                or "replace_with_" in val.lower()
                or "replace-with-" in val.lower()
            ):
                raise ValueError(
                    f"{name} cannot use a publicly known repository default or placeholder value"
                )

        if sign_sec and write_key and sign_sec == write_key:
            raise ValueError("COMMAND_AUTH_SIGNING_SECRET must be separate from COMMERCE_WRITE_KEY")
        if sign_sec and read_key and sign_sec == read_key:
            raise ValueError(
                "COMMAND_AUTH_SIGNING_SECRET must be separate from COMMERCE_SERVICE_KEY"
            )

        # In deployed environments, autonomous recovery strictly requires >= 256-bit (32+ char)
        # explicit signing secret and write key. If autonomous_recovery_enabled=False, read-only
        # operation continues safely without signing/write credentials.
        if self.autonomous_recovery_enabled and env_norm not in LOCAL_DEV_ENVIRONMENTS:
            for name, val in (
                ("COMMAND_AUTH_SIGNING_SECRET", sign_sec),
                ("COMMERCE_WRITE_KEY", write_key),
            ):
                if (
                    len(val) < 32
                    or "local-only" in val.lower()
                    or "change-me" in val.lower()
                    or val.lower() in INSECURE_DEFAULT_SECRETS
                ):
                    raise ValueError(
                        f"Deployed environment (APP_ENV='{self.app_env}') with autonomous recovery "
                        f"enabled requires an explicit strong {name} (>= 32 chars, >= 256-bit)"
                    )
        return self

    @model_validator(mode="after")
    def _validate_supervisor_heartbeat_timing(self) -> Settings:
        if self.supervisor_heartbeat_interval_seconds >= (self.supervisor_job_lease_seconds / 2.0):
            raise ValueError(
                "SUPERVISOR_HEARTBEAT_INTERVAL_SECONDS must be strictly less than half of "
                "SUPERVISOR_JOB_LEASE_SECONDS"
            )
        if self.supervisor_heartbeat_interval_seconds >= float(self.worker_heartbeat_ttl_seconds):
            raise ValueError(
                "SUPERVISOR_HEARTBEAT_INTERVAL_SECONDS must be strictly less than "
                "WORKER_HEARTBEAT_TTL_SECONDS"
            )
        return self

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip().rstrip("/") for o in self.web_origin.split(",") if o.strip()]

    @property
    def trusted_proxy_ip_set(self) -> frozenset[str]:
        return frozenset(ip.strip() for ip in self.trusted_proxy_ips.split(",") if ip.strip())

    @property
    def model_mode(self) -> ModelMode:
        has_key = bool(self.model_api_key.get_secret_value().strip())
        if self.model_provider.strip() and self.model_name.strip() and has_key:
            return ModelMode.LLM_API
        return ModelMode.DEMO_RULES


@lru_cache
def get_settings() -> Settings:
    return Settings()
