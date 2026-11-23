"""D84: DynamoDB Local on disk, and the nightly export that backs it up."""

import gzip
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import boto3
import pytest
import yaml
from moto import mock_aws

from hoopsedge.common.backup import (
    BackupError,
    export_table,
    read_export,
    restore_table,
)
from hoopsedge.common.dynamo import ensure_table
from hoopsedge.pipeline.schedule import due_jobs

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 11, 23, 4, 0, tzinfo=UTC)  # 05:00 in Lagos.
TABLE = "hoopsedge-test"


def _compose(path: Path) -> dict[str, Any]:
    document: dict[str, Any] = yaml.safe_load(path.read_text("utf-8"))
    return document


# The compose files.


def test_the_database_keeps_its_file_on_a_mounted_folder() -> None:
    service = _compose(ROOT / "docker-compose.yml")["services"]["dynamodb"]
    image, _, version = service["image"].partition(":")
    assert image == "amazon/dynamodb-local"
    assert version and version != "latest"
    command = service["command"]
    assert "-inMemory" not in command
    assert command[command.index("-dbPath") + 1] == "/home/dynamodblocal/data"
    assert "-sharedDb" in command
    assert service["volumes"] == ["./data/dynamodb:/home/dynamodblocal/data"]
    assert service["restart"] == "unless-stopped"
    assert "ports" not in service  # Only the other containers reach it.


def test_every_database_user_points_at_the_local_database_and_waits_for_it() -> None:
    services = _compose(ROOT / "docker-compose.yml")["services"]
    assert "aws-mock" not in services
    users = {
        name: service
        for name, service in services.items()
        if "AWS_ENDPOINT_URL_DYNAMODB" in (service.get("environment") or {})
    }
    assert set(users) == {"pipeline", "scheduler", "api", "live-ingest"}
    for name, service in users.items():
        assert service["environment"]["AWS_ENDPOINT_URL_DYNAMODB"] == "http://dynamodb:8000", name
        assert service["depends_on"] == {"dynamodb": {"condition": "service_healthy"}}, name


def test_the_server_file_adds_nothing_that_would_lose_the_database() -> None:
    services = _compose(ROOT / "deploy/ec2/compose.yml")["services"]
    assert "aws-mock" not in services
    assert "dynamodb" not in services  # The root file's volume applies unchanged on EC2.


# Export and restore.


@pytest.fixture
def client() -> Iterator[Any]:
    with mock_aws():
        resource = boto3.resource(
            "dynamodb",
            region_name="us-east-1",
            aws_access_key_id="x",
            aws_secret_access_key="x",  # noqa: S106 - Dummy values for moto.
        )
        ensure_table(resource, TABLE)
        yield boto3.client(
            "dynamodb",
            region_name="us-east-1",
            aws_access_key_id="x",
            aws_secret_access_key="x",  # noqa: S106 - Dummy values for moto.
        )


ITEMS: list[dict[str, Any]] = [
    {
        "pk": "TICKET#r1",
        "sk": "META",
        "odds": Decimal("1.85"),
        "legs": [{"event": "sr:match:1", "line": Decimal("49.5")}],
        "tags": {"a", "b"},
        "blob": b"\x00\x01binary",
        "dry_run": True,
        "note": None,
    },
    {"pk": "ALIAS#NCAAF", "sk": "uconn huskies", "team_id": "41", "gsi1pk": "x", "gsi1sk": "y"},
    {"pk": "CONFIG", "sk": "CURRENT", "document": {"dry_run": True, "version": Decimal(3)}},
]


def _fill(client: Any) -> None:
    table = boto3.resource(
        "dynamodb",
        region_name="us-east-1",
        aws_access_key_id="x",
        aws_secret_access_key="x",  # noqa: S106 - Dummy values for moto.
    ).Table(TABLE)
    for item in ITEMS:
        table.put_item(Item=item)


def _all(client: Any) -> list[dict[str, Any]]:
    items = client.scan(TableName=TABLE, ConsistentRead=True)["Items"]
    return sorted(items, key=lambda item: (item["pk"]["S"], item["sk"]["S"]))


def _empty(client: Any) -> None:
    for item in client.scan(TableName=TABLE)["Items"]:
        client.delete_item(TableName=TABLE, Key={"pk": item["pk"], "sk": item["sk"]})


def test_an_export_restores_every_item_exactly(client: Any, tmp_path: Path) -> None:
    _fill(client)
    before = _all(client)
    path = export_table(client, TABLE, tmp_path, NOW, keep=3)
    assert path.name == f"{TABLE}-20261123T040000Z.jsonl.gz"
    assert not list(tmp_path.glob("*.partial"))
    _empty(client)
    assert restore_table(client, TABLE, path) == len(ITEMS)
    assert _all(client) == before


def test_an_empty_table_exports_and_restores(client: Any, tmp_path: Path) -> None:
    path = export_table(client, TABLE, tmp_path, NOW, keep=3)
    assert read_export(path) == []
    assert restore_table(client, TABLE, path) == 0


def test_a_restore_refuses_a_table_that_already_has_items(client: Any, tmp_path: Path) -> None:
    _fill(client)
    path = export_table(client, TABLE, tmp_path, NOW, keep=3)
    with pytest.raises(BackupError, match="already has items"):
        restore_table(client, TABLE, path)
    assert restore_table(client, TABLE, path, into_nonempty=True) == len(ITEMS)


def _rewrite(path: Path, lines: list[str]) -> None:
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        handle.write("".join(lines))


def _lines(path: Path) -> list[str]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return handle.readlines()


def test_an_export_cut_short_is_refused(client: Any, tmp_path: Path) -> None:
    _fill(client)
    path = export_table(client, TABLE, tmp_path, NOW, keep=3)
    _rewrite(path, _lines(path)[:-1])  # No trailer.
    with pytest.raises(BackupError, match="cut short"):
        read_export(path)


def test_an_export_missing_an_item_is_refused(client: Any, tmp_path: Path) -> None:
    _fill(client)
    path = export_table(client, TABLE, tmp_path, NOW, keep=3)
    lines = _lines(path)
    _rewrite(path, lines[1:])  # Trailer still says three.
    with pytest.raises(BackupError, match="3 items but holds 2"):
        restore_table(client, TABLE, path, into_nonempty=True)


def test_a_file_that_is_not_an_export_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "other.jsonl.gz"
    _rewrite(path, [json.dumps({"_trailer": {"format": "other", "items": 0}}) + "\n"])
    with pytest.raises(BackupError, match="format"):
        read_export(path)
    broken = tmp_path / "broken.jsonl.gz"
    broken.write_bytes(b"not gzip")
    with pytest.raises(BackupError, match="can't be read"):
        read_export(broken)


def test_only_the_newest_exports_are_kept(client: Any, tmp_path: Path) -> None:
    written = [
        export_table(client, TABLE, tmp_path, NOW + timedelta(days=day), keep=2) for day in range(4)
    ]
    assert sorted(tmp_path.glob("*.jsonl.gz")) == written[-2:]
    with pytest.raises(BackupError, match="keep"):
        export_table(client, TABLE, tmp_path, NOW, keep=0)


# The nightly job.


def _names(now: datetime, done: dict[str, str] | None = None) -> set[str]:
    return {job.name for job in due_jobs(now, {"grade": now.isoformat(), **(done or {})})}


def test_the_export_runs_at_five_lagos_once_a_day() -> None:
    assert _names(NOW) == {"backup"}
    assert _names(NOW - timedelta(minutes=1)) == set()
    assert _names(NOW, {"backup": "2026-11-23"}) == set()
    job = next(job for job in due_jobs(NOW, {}) if job.name == "backup")
    assert job.command[-2:] == ("hoopsedge.common.backup", "export")


def test_the_export_time_can_be_moved_in_settings() -> None:
    moved = {"backup": "06:00"}
    assert "backup" not in {job.name for job in due_jobs(NOW, {}, moved)}
    later = NOW + timedelta(hours=1)
    assert "backup" in {job.name for job in due_jobs(later, {}, moved)}
