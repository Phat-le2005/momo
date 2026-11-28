"""Initialize a private, dedicated WSL environment for the VNPAY Sandbox stack."""

from __future__ import annotations

import getpass
import os
import re
import sys
import warnings
from pathlib import Path

from init_local_secrets import generate_local_env

PRIVATE_DIR = Path("/var/lib/finshop-vnpay-sandbox")


def main() -> None:
    if not sys.stdin.isatty() or not sys.stderr.isatty() or os.geteuid() != 0:
        sys.exit("Run this interactively in Ubuntu WSL as root.")
    os.umask(0o077)
    PRIVATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    if PRIVATE_DIR.is_symlink() or PRIVATE_DIR.stat().st_uid != 0:
        sys.exit(
            "Sandbox env directory must be owned by root and must not be a symlink."
        )
    PRIVATE_DIR.chmod(0o700)
    target = PRIVATE_DIR / "live.env"
    staging = PRIVATE_DIR / ".live.env-initializing"
    if target.exists() or staging.exists():
        sys.exit("Sandbox credential file already exists or setup is in progress.")

    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        merchant = getpass.getpass("VNPAY Sandbox TmnCode (hidden): ").strip()
        secret = getpass.getpass("VNPAY Sandbox HashSecret (hidden): ").strip()
    if not re.fullmatch(r"[A-Za-z0-9]{8}", merchant):
        sys.exit("TmnCode must be eight ASCII alphanumeric characters.")
    if (
        len(secret) < 16
        or not secret.isascii()
        or not secret.isprintable()
        or "'" in secret
    ):
        sys.exit("HashSecret format is invalid. Check the merchant email.")

    fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    try:
        generate_local_env(staging)
        with staging.open("a", encoding="utf-8") as out:
            out.write(f"COMMERCE_VNPAY_TMN_CODE='{merchant}'\n")
            out.write(f"COMMERCE_VNPAY_HASH_SECRET='{secret}'\n")
        os.link(staging, target)
    finally:
        staging.unlink(missing_ok=True)
    print("Private Sandbox credentials saved. No service has been started.")


if __name__ == "__main__":
    main()
