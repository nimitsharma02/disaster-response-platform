"""Export the OpenAPI document to docs/openapi.json.

The spec is generated FROM the code, never hand-maintained, so it cannot drift
away from what the API actually does.

    docker compose exec api python -m scripts.export_openapi
"""

from __future__ import annotations

import json
from pathlib import Path

from app.main import create_app


def main() -> None:
    app = create_app()
    spec = app.openapi()

    out = Path(__file__).resolve().parent.parent / "docs" / "openapi.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(spec, indent=2, sort_keys=False) + "\n", encoding="utf-8")

    paths = len(spec.get("paths", {}))
    schemas = len(spec.get("components", {}).get("schemas", {}))
    print(f"Wrote {out}  ({paths} paths, {schemas} schemas)")


if __name__ == "__main__":
    main()
