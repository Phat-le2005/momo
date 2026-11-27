"""Safe, idempotent, non-destructive local credential initializer for FinResolve + MiniStore.

Behavior (Prompt 14.4 Fix 6):
- Preserves existing `.env` comments, custom deployment settings, ports, DB URLs, and
  LLM configuration (`MODEL_PROVIDER`, `MODEL_NAME`, `MODEL_API_KEY`).
- Generates ONLY missing required credentials or replaces known insecure/weak defaults.
- Requires explicit `--rotate` (or `rotate=True`) to rotate already-valid strong secrets.
- Keeps synchronized credentials aligned (`COMMERCE_SERVICE_KEY == COMMERCE_SERVICE_API_KEY`).
- Writes atomically via temp file + `os.replace` with restrictive file permissions (`0o600`).
- NEVER prints secret values to stdout/stderr.
"""

from __future__ import annotations

import argparse
import os
import secrets
import tempfile
from pathlib import Path

KNOWN_INSECURE_VALUES: frozenset[str] = frozenset(
    {
        "",
        "change-me",
        "change-me-local-only",
        "default",
        "secret",
        "password",
        "123456",
        "demo-secret",
        "finresolve-demo-manager-hmac-signing-key-local-only",
        "finresolve-dev-only-hmac-approval-secret-2026-change-me",
        "finresolve_docker_hmac_signing_key_v1_9f8e7d6c5b4a3f2e",
        "finresolve-local-only-command-auth-hmac-secret-2026-change-me",
        "finshop-local-only-service-write-key-2026-change-me",
        "finshop-local-only-dev-hmac-secret-key-2026-change-me",
        "finshop-local-only-service-connector-key-2026-change-me",
        "finshop-local-only-mock-provider-secret-2026-change-me",
        "replace_with_strong_random_32_plus_char_hmac_secret_key",
        "replace-with-strong-random-32-plus-char-hmac-secret-key",
    }
)

MANAGED_HEX_SECRETS: tuple[str, ...] = (
    "DEMO_APPROVAL_SECRET",
    "COMMERCE_AUTH_SECRET",
    "COMMERCE_SERVICE_API_KEY",
    "COMMERCE_WRITE_KEY",
    "COMMAND_AUTH_SIGNING_SECRET",
    "COMMERCE_MOCK_PROVIDER_SECRET",
    "EXPERIENCE_OPERATOR_TOKEN",
)

STATIC_DEFAULTS: dict[str, str] = {
    "COMMERCE_VNPAY_ENABLED": "false",
    "COMMERCE_VNPAY_REFUND_ENABLED": "false",
}


def _is_valid_secret(value: str, *, min_len: int = 32) -> bool:
    clean = value.strip()
    if len(clean) < min_len:
        return False
    return clean.lower() not in KNOWN_INSECURE_VALUES


def _parse_env_file(path: Path) -> tuple[list[str], dict[str, str]]:
    if not path.exists():
        return [], {}
    raw_lines = path.read_text(encoding="utf-8").splitlines()
    existing: dict[str, str] = {}
    for line in raw_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        k, v = stripped.split("=", 1)
        existing[k.strip()] = v.strip()
    return raw_lines, existing


def generate_local_env(
    target_path: Path | None = None,
    *,
    rotate: bool = False,
) -> tuple[Path, dict[str, str]]:
    """Idempotently initialize or rotate local credentials without destroying existing config.

    Returns `(env_file_path, change_summary)` where `change_summary` maps key -> action
    (`preserved`, `generated`, `rotated`, `synced`) and NEVER contains secret values.
    """
    env_file = (target_path or (Path(__file__).resolve().parent / ".env")).resolve()
    env_file.parent.mkdir(parents=True, exist_ok=True)
    raw_lines, existing = _parse_env_file(env_file)

    updated: dict[str, str] = dict(existing)
    summary: dict[str, str] = {}

    for key in MANAGED_HEX_SECRETS:
        cur = existing.get(key, "")
        if rotate or not _is_valid_secret(cur, min_len=32):
            updated[key] = secrets.token_hex(32)
            summary[key] = "rotated" if (rotate and cur) else "generated"
        else:
            summary[key] = "preserved"

    # Keep FinResolve read key strictly synchronized with MiniStore service read key
    if updated.get("COMMERCE_SERVICE_KEY") != updated["COMMERCE_SERVICE_API_KEY"]:
        updated["COMMERCE_SERVICE_KEY"] = updated["COMMERCE_SERVICE_API_KEY"]
        summary["COMMERCE_SERVICE_KEY"] = "synced"
    else:
        summary["COMMERCE_SERVICE_KEY"] = "preserved"

    # Merchant test password (>= 12 chars)
    pw_key = "COMMERCE_TEST_MERCHANT_PASSWORD"
    cur_pw = existing.get(pw_key, "")
    if rotate or not _is_valid_secret(cur_pw, min_len=12):
        updated[pw_key] = f"FinShop!{secrets.token_urlsafe(12)}"
        summary[pw_key] = "rotated" if (rotate and cur_pw) else "generated"
    else:
        summary[pw_key] = "preserved"

    for key, default_val in STATIC_DEFAULTS.items():
        if key not in updated or not updated[key].strip():
            updated[key] = default_val
            summary[key] = "generated"
        else:
            summary[key] = "preserved"

    # Rebuild file preserving original line order, comments, and custom keys
    out_lines: list[str] = []
    written_keys: set[str] = set()
    for line in raw_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            out_lines.append(line)
            continue
        k, _ = stripped.split("=", 1)
        k_clean = k.strip()
        if k_clean in updated:
            out_lines.append(f"{k_clean}={updated[k_clean]}")
            written_keys.add(k_clean)
        else:
            out_lines.append(line)

    for k, v in updated.items():
        if k not in written_keys:
            out_lines.append(f"{k}={v}")

    content = "\n".join(out_lines) + "\n"

    # Atomic write via temp file in same directory + restrictive permissions
    fd, tmp_name = tempfile.mkstemp(prefix=".env.tmp.", dir=str(env_file.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
        try:
            os.chmod(tmp_path, 0o600)
        except OSError:
            pass
        os.replace(tmp_path, env_file)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    return env_file, summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Idempotently initialize local FinResolve secrets."
    )
    parser.add_argument(
        "--rotate",
        action="store_true",
        help="Explicitly rotate existing strong managed secrets (preserves LLM & custom config).",
    )
    args = parser.parse_args()
    out, actions = generate_local_env(rotate=args.rotate)
    print(f"Updated {out} (secrets redacted): {actions}")
