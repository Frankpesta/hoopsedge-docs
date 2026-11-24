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
    """Run now and Rebook, as background processes on this server (D98)."""

    @staticmethod
    def _launch(*arguments: str) -> None:
        subprocess.Popen(  # noqa: S603
            [sys.executable, "-m", "hoopsedge.v2.slate", *arguments], env=os.environ.copy()
        )

    def start(self, sport: str, slate: date) -> None:
        self._launch("run", "--sport", sport, "--date", slate.isoformat(), "--trigger", "dashboard")

    def rebook(self, sport: str, slate: date, ticket_id: str) -> None:
        self._launch(
            "rebook", "--sport", sport, "--date", slate.isoformat(), "--ticket-id", ticket_id
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
