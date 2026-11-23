"""Nightly export of the DynamoDB table, and restore from one (D84).

    python -m hoopsedge.common.backup export [--keep 14]
    python -m hoopsedge.common.backup restore <export>.jsonl.gz [--into-nonempty]

An export is gzipped JSON lines in DynamoDB's typed format, so numbers, sets and binary values come
back exactly. Its last line is a trailer with the item count: a file without one, or with the wrong
count, was cut short and is refused. Files are written under a temporary name and renamed once
complete. Only the newest `keep` exports are kept.
"""

import argparse
import base64
import gzip
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hoopsedge.common.dynamo import connect, connect_client, ensure_table, table_name

FORMAT = "hoopsedge-dynamodb-export/1"
DEFAULT_KEEP = 14
DEFAULT_DATA_DIR = "/workspace/data"
SUBFOLDER = Path("backups") / "dynamodb"


class BackupError(RuntimeError):
    """An export or restore that can't be trusted. Never ignored."""


def _encode(value: Any) -> Any:
    """Make a typed attribute value JSON-safe. Binary becomes base64 text."""
    if isinstance(value, dict):
        if set(value) == {"B"}:
            return {"B": base64.b64encode(bytes(value["B"])).decode("ascii")}
        if set(value) == {"BS"}:
            return {"BS": [base64.b64encode(bytes(item)).decode("ascii") for item in value["BS"]]}
        return {key: _encode(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_encode(item) for item in value]
    return value


def _decode(value: Any) -> Any:
    if isinstance(value, dict):
        if set(value) == {"B"}:
            return {"B": base64.b64decode(value["B"])}
        if set(value) == {"BS"}:
            return {"BS": [base64.b64decode(item) for item in value["BS"]]}
        return {key: _decode(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_decode(item) for item in value]
    return value


def export_table(client: Any, table: str, folder: Path, now: datetime, keep: int) -> Path:
    """Write every item of `table` to a new file in `folder`. Keep only the newest `keep` files."""
    if keep < 1:
        raise BackupError("keep must be at least 1")
    folder.mkdir(parents=True, exist_ok=True)
    stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    final = folder / f"{table}-{stamp}.jsonl.gz"
    partial = final.with_name(final.name + ".partial")
    count = 0
    with gzip.open(partial, "wt", encoding="utf-8") as handle:
        for page in client.get_paginator("scan").paginate(TableName=table, ConsistentRead=True):
            for item in page["Items"]:
                handle.write(json.dumps(_encode(item), sort_keys=True) + "\n")
                count += 1
        trailer = {"format": FORMAT, "table": table, "exported_at": now.isoformat(), "items": count}
        handle.write(json.dumps({"_trailer": trailer}) + "\n")
    read_back = read_export(partial)
    if len(read_back) != count:
        raise BackupError(f"{partial.name} reads back {len(read_back)} items, wrote {count}")
    partial.replace(final)
    for old in sorted(folder.glob(f"{table}-*.jsonl.gz"))[:-keep]:
        old.unlink()
    return final


def read_export(path: Path) -> list[dict[str, Any]]:
    """The items in an export. Refuses a file that was cut short or isn't an export."""
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            lines = [json.loads(line) for line in handle if line.strip()]
    except (OSError, EOFError, json.JSONDecodeError) as error:
        raise BackupError(f"{path.name} can't be read: {error}") from error
    if not lines or "_trailer" not in lines[-1]:
        raise BackupError(f"{path.name} has no trailer, so it was cut short")
    trailer = lines[-1]["_trailer"]
    items = lines[:-1]
    if trailer.get("format") != FORMAT:
        raise BackupError(f"{path.name} has format {trailer.get('format')!r}, expected {FORMAT}")
    if trailer.get("items") != len(items):
        raise BackupError(f"{path.name} says {trailer.get('items')} items but holds {len(items)}")
    return [_decode(item) for item in items]


def restore_table(client: Any, table: str, path: Path, *, into_nonempty: bool = False) -> int:
    """Put every item of an export into `table`. Refuses a table that already has items, unless
    told otherwise, so a restore can't quietly mix two states."""
    items = read_export(path)
    if not into_nonempty and client.scan(TableName=table, Limit=1)["Items"]:
        raise BackupError(f"{table} already has items. Restore into an empty table")
    for item in items:
        client.put_item(TableName=table, Item=item)
    return len(items)


def _alert(client: Any, table: str, now: datetime, message: str) -> None:
    """Best effort: when the database itself is down, the scheduler's log keeps the failure."""
    try:
        client.put_item(
            TableName=table,
            Item={
                "pk": {"S": "ALERT"},
                "sk": {"S": f"{now.astimezone(UTC).isoformat()}#backup-failed"},
                "severity": {"S": "CRITICAL"},
                "message": {"S": message[:1000]},
                "ref": {"S": "backup"},
            },
        )
    except Exception as error:  # noqa: BLE001 - The original failure is reported either way.
        print(f"Couldn't record the alert either: {error}", file=sys.stderr, flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hoopsedge.common.backup")
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export", help="Export the table to data/backups/dynamodb")
    export.add_argument("--keep", type=int, default=DEFAULT_KEEP)
    restore = commands.add_parser("restore", help="Restore an export into the table")
    restore.add_argument("path", type=Path)
    restore.add_argument("--into-nonempty", action="store_true")
    arguments = parser.parse_args(argv)

    table = table_name()
    ensure_table(connect(), table)
    client = connect_client()
    now = datetime.now(UTC)
    if arguments.command == "export":
        folder = Path(os.environ.get("HOOPSEDGE_DATA_DIR", DEFAULT_DATA_DIR)) / SUBFOLDER
        try:
            written = export_table(client, table, folder, now, arguments.keep)
        except Exception as error:
            _alert(client, table, now, f"DynamoDB export failed: {error}")
            raise
        print(f"Exported {table} to {written}", flush=True)
        return 0
    count = restore_table(client, table, arguments.path, into_nonempty=arguments.into_nonempty)
    print(f"Restored {count} items into {table} from {arguments.path}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
