"""Safe local credential initializer for FinResolve + MiniStore Docker Compose.

Generates cryptographically random >= 256-bit secrets into the git-ignored
`FinResolve/infra/.env` file WITHOUT printing any secret values to stdout/logs.
"""

from __future__ import annotations

import secrets
from pathlib import Path


def generate_local_env(target_path: Path | None = None) -> Path:
    env_file = target_path or (Path(__file__).resolve().parent / ".env")
    entries = {
        "DEMO_APPROVAL_SECRET": secrets.token_hex(32),
        "COMMERCE_AUTH_SECRET": secrets.token_hex(32),
        "COMMERCE_SERVICE_API_KEY": secrets.token_hex(32),
        "COMMERCE_SERVICE_KEY": "",  # populated below to match COMMERCE_SERVICE_API_KEY
        "COMMERCE_WRITE_KEY": secrets.token_hex(32),
        "COMMAND_AUTH_SIGNING_SECRET": secrets.token_hex(32),
        "COMMERCE_MOCK_PROVIDER_SECRET": secrets.token_hex(32),
        "COMMERCE_TEST_MERCHANT_PASSWORD": f"FinShop!{secrets.token_urlsafe(12)}",
        "COMMERCE_VNPAY_ENABLED": "false",
        "COMMERCE_VNPAY_REFUND_ENABLED": "false",
    }
    entries["COMMERCE_SERVICE_KEY"] = entries["COMMERCE_SERVICE_API_KEY"]

    lines = [f"{k}={v}" for k, v in entries.items()]
    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return env_file


if __name__ == "__main__":
    out = generate_local_env()
    print(f"Generated strong 256-bit local credentials in {out} (secrets redacted).")
