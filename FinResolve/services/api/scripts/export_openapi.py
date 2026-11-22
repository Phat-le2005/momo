"""Write the current OpenAPI schema to services/api/openapi.json (frozen contract snapshot).

Usage (from services/api):  python -m scripts.export_openapi
"""

from __future__ import annotations

import json
from pathlib import Path

from app.main import create_app


def main() -> None:
    target = Path(__file__).resolve().parents[1] / "openapi.json"
    schema = create_app().openapi()
    target.write_text(json.dumps(schema, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {target}")


if __name__ == "__main__":
    main()
