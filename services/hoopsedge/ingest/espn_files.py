"""ESPN fetching and file plumbing shared by the v2 ingest (moved out of v1 `backfill.py`, D96).

A 401, 403, 451 or non-JSON response halts every ESPN job: a file records why, and nothing
fetches from ESPN again until the owner has looked and run `python -m hoopsedge.ingest.scores
resume`. The halt file keeps v1's path, so a halt recorded before the move still counts.
"""

import gzip
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import polars as pl

from hoopsedge.common.http import HttpFetchError, NotJsonError, PoliteClient
from hoopsedge.ingest.espn_coverage import EASTERN

BLOCK_STATUS = frozenset({401, 403, 451})

Log = Callable[[str], None]


class BlockedError(RuntimeError):
    """ESPN refused us. Every ESPN job stays stopped until the owner resumes."""


@dataclass(frozen=True)
class Fetcher:
    data_dir: Path
    client: PoliteClient
    today_et: date
    log: Log = print


def today_eastern() -> date:
    return datetime.now(UTC).astimezone(EASTERN).date()


def write_gz(path: Path, document: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(gzip.compress(json.dumps(document).encode("utf-8")))
    os.replace(temporary, path)


def read_gz(path: Path) -> Any:
    return json.loads(gzip.decompress(path.read_bytes()))


def write_parquet(frame: pl.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    frame.write_parquet(temporary)
    os.replace(temporary, path)


def halt_path(data_dir: Path) -> Path:
    return data_dir / "backfill" / "HALTED"


def assert_not_halted(data_dir: Path) -> None:
    path = halt_path(data_dir)
    if path.exists():
        raise BlockedError(f"ESPN fetching is halted: {path.read_text('utf-8')}")


def fetch_json(fetcher: Fetcher, url: str, params: dict[str, str] | None = None) -> Any:
    """GET, and turn any block into a halt that lasts until the owner clears it."""
    try:
        return fetcher.client.get_json(url, params)
    except HttpFetchError as error:
        if isinstance(error, NotJsonError) or error.status in BLOCK_STATUS:
            path = halt_path(fetcher.data_dir)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{datetime.now(UTC).isoformat()} {error}", "utf-8")
            raise BlockedError(str(error)) from error
        raise
