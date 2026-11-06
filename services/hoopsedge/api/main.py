"""Running the API: locally with Uvicorn, and on Lambda through Mangum (docs/09).

    docker compose --profile manual up -d api
    python -m hoopsedge.api.main export     # Writes docs/api/openapi.json, for the dashboard.

The Lambda entry point is `hoopsedge.api.main.handler`.
"""

import json
import os
import subprocess  # noqa: S404 - Starts this package's own pipeline, with fixed arguments.
import sys
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from mangum import Mangum

from hoopsedge.api.app import Deps, create_app, openapi_document
from hoopsedge.api.store import ApiStore, Artifacts
from hoopsedge.common.dynamo import connect, ensure_table, table_name

DEFAULT_DATA_DIR = "/workspace/data"
OPENAPI_FILE = Path(__file__).resolve().parents[3] / "docs" / "api" / "openapi.json"


class LocalRunStarter:
    """Run now, as a background process. Step Functions takes this over with Phase 1."""

    def start(self, league: str, slate: date, run_id: str) -> None:
        subprocess.Popen(  # noqa: S603
            [
                sys.executable,
                "-m",
                "hoopsedge.pipeline.slate",
                "run",
                "--league",
                league,
                "--date",
                slate.isoformat(),
                "--run-id",
                run_id,
                "--trigger",
                "dashboard",
            ],  # fmt: skip
            env=os.environ.copy(),
        )


def build_app() -> FastAPI:
    data_dir = Path(os.environ.get("HOOPSEDGE_DATA_DIR", DEFAULT_DATA_DIR))
    resource = connect()
    ensure_table(resource, table_name())
    deps = Deps(ApiStore(resource.Table(table_name())), Artifacts(data_dir), LocalRunStarter())
    return create_app(deps)


_lambda_app: Mangum | None = None


def handler(event: dict[str, Any], context: Any) -> Any:
    """Built once per container, so a warm request does no setup."""
    global _lambda_app  # noqa: PLW0603
    if _lambda_app is None:
        _lambda_app = Mangum(build_app(), lifespan="off")
    return _lambda_app(event, context)


def export_openapi(path: Path = OPENAPI_FILE) -> dict[str, Any]:
    deps = Deps(None, Artifacts(Path()), LocalRunStarter())
    document = openapi_document(create_app(deps))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=1, sort_keys=True) + "\n", "utf-8")
    return document


if __name__ == "__main__":
    if sys.argv[1:] == ["export"]:
        export_openapi()
        print(OPENAPI_FILE)
    else:
        sys.exit("Usage: python -m hoopsedge.api.main export")
