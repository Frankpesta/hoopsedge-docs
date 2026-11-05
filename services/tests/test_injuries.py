"""NBA injury snapshots to curated rows (docs/03 section 2.3). Append-only and repeatable."""

import gzip
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl

from hoopsedge.ingest.injuries import parse_injuries, sync_injuries

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "espn"
CAPTURED = datetime(2026, 11, 5, 15, 56, 7, tzinfo=UTC)


def real_document() -> dict[str, Any]:
    document: dict[str, Any] = json.loads((FIXTURES / "nba-injuries.json").read_text("utf-8"))
    return document


def row(player: str | None, status: str = "Out", comment: str = "Knee.") -> dict[str, Any]:
    links = [{"href": f"https://www.espn.com/nba/player/_/id/{player}/x"}] if player else []
    return {"status": status, "shortComment": comment, "athlete": {"links": links}}


def team(team_id: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"id": team_id, "displayName": "T", "injuries": rows}


def store(data_dir: Path, name: str, document: Any, complete: bool = True) -> Path:
    path = data_dir / f"raw/espn/injuries/2026/11/05/{name}.json.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    envelope = {
        "started_at": CAPTURED.isoformat(),
        "captured_at": CAPTURED.isoformat(),
        "complete": complete,
        "responses": [document],
    }
    path.write_bytes(gzip.compress(json.dumps(envelope).encode("utf-8")))
    return path


def parts(data_dir: Path) -> list[Path]:
    return sorted((data_dir / "curated/injuries/league=NBA/season=2027").glob("part-*.parquet"))


def test_rows_keep_the_snapshot_time_player_team_status_and_comment() -> None:
    document = {"injuries": [team("1", [row("3146557", "Day-To-Day", "Hip strain.")])]}
    rows, unreadable = parse_injuries(document, CAPTURED)
    assert unreadable == 0
    assert rows == [
        {
            "captured_at": CAPTURED,
            "player_id": "3146557",
            "team_id": "1",
            "status": "Day-To-Day",
            "detail": "Hip strain.",
        }
    ]


def test_real_snapshot_parses_every_row() -> None:
    document = real_document()
    expected = sum(len(group["injuries"]) for group in document["injuries"])
    rows, unreadable = parse_injuries(document, CAPTURED)
    assert expected > 0
    assert len(rows) == expected
    assert unreadable == 0
    assert all(item["player_id"].isdigit() for item in rows)
    assert {item["team_id"] for item in rows} == {g["id"] for g in document["injuries"]}


def test_a_row_without_a_player_id_is_counted_not_guessed() -> None:
    document = {"injuries": [team("1", [row(None), row("7")])]}
    rows, unreadable = parse_injuries(document, CAPTURED)
    assert [item["player_id"] for item in rows] == ["7"]
    assert unreadable == 1


def test_sync_writes_one_part_per_snapshot_in_the_captured_season(tmp_path: Path) -> None:
    store(tmp_path, "injuries-1", {"injuries": [team("1", [row("7")])]})
    summary = sync_injuries(tmp_path)
    assert summary == {"new_snapshots": 1, "rows": 1, "unreadable_rows": 0, "bad_snapshots": []}
    frame = pl.read_parquet(parts(tmp_path)[0])
    assert frame["player_id"].to_list() == ["7"]
    assert frame.schema["captured_at"] == pl.Datetime("us", "UTC")


def test_sync_is_repeatable_and_appends_only_new_snapshots(tmp_path: Path) -> None:
    store(tmp_path, "injuries-1", {"injuries": [team("1", [row("7")])]})
    sync_injuries(tmp_path)
    first = parts(tmp_path)[0].stat().st_mtime_ns
    assert sync_injuries(tmp_path)["new_snapshots"] == 0
    assert parts(tmp_path)[0].stat().st_mtime_ns == first

    store(tmp_path, "injuries-2", {"injuries": [team("2", [row("8")])]})
    assert sync_injuries(tmp_path)["new_snapshots"] == 1
    assert len(parts(tmp_path)) == 2


def test_a_snapshot_with_nobody_injured_is_kept_because_it_means_everyone_is_available(
    tmp_path: Path,
) -> None:
    store(tmp_path, "injuries-1", {"injuries": []})
    assert sync_injuries(tmp_path)["new_snapshots"] == 1
    frame = pl.read_parquet(parts(tmp_path)[0])
    assert frame.is_empty()
    assert frame.columns == ["captured_at", "player_id", "team_id", "status", "detail"]


def test_a_bad_or_incomplete_snapshot_is_reported_once_and_set_aside(
    tmp_path: Path,
) -> None:
    store(tmp_path, "injuries-1", {"unexpected": []})
    store(tmp_path, "injuries-2", {"injuries": []}, complete=False)
    summary = sync_injuries(tmp_path)
    assert summary["new_snapshots"] == 0
    assert sorted(summary["bad_snapshots"]) == ["injuries-1", "injuries-2"]
    assert parts(tmp_path) == []
    assert sync_injuries(tmp_path)["bad_snapshots"] == []
    marker = tmp_path / "curated/_rejects/injuries/injuries-2.txt"
    assert "incomplete" in marker.read_text("utf-8")
