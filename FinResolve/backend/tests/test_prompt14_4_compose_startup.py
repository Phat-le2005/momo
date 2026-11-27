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


def test_init_local_secrets_is_idempotent_non_destructive_and_supports_rotation(
    tmp_path: Path,
) -> None:
    """Prompt 14.4 Fix 6: Repeated init preserves LLM config, custom ports & valid secrets;
    replaces weak defaults; explicit rotate=True rotates managed keys in sync.
    """
    import importlib.util

    script_path = Path(__file__).resolve().parents[2] / "infra" / "init_local_secrets.py"
    spec = importlib.util.spec_from_file_location("init_local_secrets", script_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    env_file = tmp_path / ".env"
    env_file.write_text(
        "# Custom deployment header\n"
        "MODEL_PROVIDER=gemini\n"
        "MODEL_NAME=gemini-2.5-flash\n"
        "MODEL_API_KEY=user-custom-llm-key-keep-me\n"
        "API_PORT=8088\n"
        "COMMAND_AUTH_SIGNING_SECRET=change-me\n",
        encoding="utf-8",
    )

    # First run: replaces weak COMMAND_AUTH_SIGNING_SECRET, generates missing, preserves LLM/custom
    sign_key = "COMMAND_AUTH_SIGNING_SECRET"
    _, sum1 = mod.generate_local_env(env_file, rotate=False)
    assert sum1[sign_key] == "generated"
    raw1 = dict(
        line.split("=", 1)
        for line in env_file.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#") and "=" in line
    )
    assert raw1["MODEL_PROVIDER"] == "gemini"
    assert raw1["MODEL_NAME"] == "gemini-2.5-flash"
    assert raw1["MODEL_API_KEY"] == "user-custom-llm-key-keep-me"
    assert raw1["API_PORT"] == "8088"
    assert len(raw1[sign_key]) >= 64
    assert raw1["COMMERCE_SERVICE_KEY"] == raw1["COMMERCE_SERVICE_API_KEY"]

    # Second run (idempotent): preserves ALL existing valid secrets & custom settings
    _, sum2 = mod.generate_local_env(env_file, rotate=False)
    assert sum2[sign_key] == "preserved"
    assert sum2["COMMERCE_WRITE_KEY"] == "preserved"
    raw2 = dict(
        line.split("=", 1)
        for line in env_file.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#") and "=" in line
    )
    assert raw2 == raw1

    # Explicit rotation: rotates managed secrets in sync while preserving LLM & custom settings
    _, sum3 = mod.generate_local_env(env_file, rotate=True)
    assert sum3[sign_key] == "rotated"
    raw3 = dict(
        line.split("=", 1)
        for line in env_file.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#") and "=" in line
    )
    assert raw3["COMMAND_AUTH_SIGNING_SECRET"] != raw2["COMMAND_AUTH_SIGNING_SECRET"]
    assert raw3["COMMERCE_SERVICE_KEY"] == raw3["COMMERCE_SERVICE_API_KEY"]
    assert raw3["MODEL_API_KEY"] == "user-custom-llm-key-keep-me"
    assert raw3["API_PORT"] == "8088"
