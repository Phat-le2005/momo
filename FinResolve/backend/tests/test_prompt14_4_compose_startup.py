"""Prompt 14.4 Fix 1 Tests: Docker Compose Least-Privilege Service Startup Validation."""

from __future__ import annotations

import secrets
from pathlib import Path

import pytest

from app.core.config import Settings


def _load_compose_text() -> str:
    compose_path = Path(__file__).resolve().parents[2] / "infra" / "docker-compose.yml"
    return compose_path.read_text(encoding="utf-8")


def test_compose_least_privilege_finresolve_services_validate_cleanly() -> None:
    """Prove api (read-only), monitor-worker (read-only), and worker (signer) all validate
    under APP_ENV=docker without weakening validation or restoring default secrets.
    """
    text = _load_compose_text()
    assert 'AUTONOMOUS_RECOVERY_ENABLED: "false"' in text
    assert 'AUTONOMOUS_RECOVERY_ENABLED: "true"' in text
    assert 'COMMERCE_RECOVERY_WRITE_ENABLED: "false"' in text
    assert 'COMMERCE_RECOVERY_WRITE_ENABLED: "true"' in text
    assert "finshop_private_receipts:/var/lib/finshop/private_receipts" in text
    assert "127.0.0.1:${POSTGRES_PORT:-5433}:5432" in text
    assert "127.0.0.1:${COMMERCE_POSTGRES_PORT:-5434}:5432" in text

    demo_sec = secrets.token_hex(32)
    read_key = secrets.token_hex(32)
    write_key = secrets.token_hex(32)
    sign_sec = secrets.token_hex(32)

    # 1. FinResolve API: read-only mode (no signing secret, no write key) -> starts cleanly!
    api_cfg = Settings(
        app_env="docker",
        demo_approval_secret=demo_sec,  # type: ignore[arg-type]
        autonomous_recovery_enabled=False,
        commerce_service_base_url="http://commerce-api:8100",
        commerce_service_key=read_key,  # type: ignore[arg-type]
        commerce_write_key="",  # type: ignore[arg-type]
        command_auth_signing_secret="",  # type: ignore[arg-type]
    )
    assert api_cfg.autonomous_recovery_enabled is False

    # 2. Monitor Worker: read + status lookup only (no signing secret) -> starts cleanly!
    mon_cfg = Settings(
        app_env="docker",
        demo_approval_secret=demo_sec,  # type: ignore[arg-type]
        autonomous_recovery_enabled=False,
        continuous_monitor_enabled=True,
        commerce_service_base_url="http://commerce-api:8100",
        commerce_service_key=read_key,  # type: ignore[arg-type]
        commerce_write_key=write_key,  # type: ignore[arg-type]
        command_auth_signing_secret="",  # type: ignore[arg-type]
    )
    assert mon_cfg.autonomous_recovery_enabled is False

    # 3. Supervisor Worker: autonomous_recovery_enabled=True with strong separate keys -> starts!
    sup_cfg = Settings(
        app_env="docker",
        demo_approval_secret=demo_sec,  # type: ignore[arg-type]
        autonomous_recovery_enabled=True,
        commerce_service_base_url="http://commerce-api:8100",
        commerce_service_key=read_key,  # type: ignore[arg-type]
        commerce_write_key=write_key,  # type: ignore[arg-type]
        command_auth_signing_secret=sign_sec,  # type: ignore[arg-type]
    )
    assert sup_cfg.autonomous_recovery_enabled is True

    # 4. Supervisor Worker missing signing secret under APP_ENV=docker -> MUST still fail closed!
    with pytest.raises(ValueError, match="COMMAND_AUTH_SIGNING_SECRET"):
        Settings(
            app_env="docker",
            demo_approval_secret=demo_sec,  # type: ignore[arg-type]
            autonomous_recovery_enabled=True,
            commerce_write_key=write_key,  # type: ignore[arg-type]
            command_auth_signing_secret="",  # type: ignore[arg-type]
        )
