"""NBA injury snapshots to curated rows (docs/03 section 2.3).

One Parquet part per raw snapshot, never changed afterwards, so point-in-time availability can
be rebuilt. It reads the collector's snapshots too, so history from before live ingest counts.
"""

import gzip
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import polars as pl

from hoopsedge.ingest.curated import write_parquet

_PLAYER_ID = re.compile(r"/id/(\d+)")
INJURY_SCHEMA: dict[str, Any] = {
    "captured_at": pl.Datetime("us", "UTC"),
    "player_id": pl.String,
    "team_id": pl.String,
    "status": pl.String,
    "detail": pl.String,
}


def season_of(moment: datetime) -> int:
    """The season a snapshot belongs to, by closing year. October onward is the next one."""
    return moment.year + 1 if moment.month >= 10 else moment.year


def _player_id(row: dict[str, Any]) -> str | None:
    for link in row.get("athlete", {}).get("links") or []:
        found = _PLAYER_ID.search(str(link.get("href", "")))
        if found:
            return found.group(1)
    return None


def parse_injuries(
    document: dict[str, Any], captured_at: datetime
) -> tuple[list[dict[str, Any]], int]:
    """Return rows, and how many had no readable player ID. Those aren't guessed."""
    rows: list[dict[str, Any]] = []
    unreadable = 0
    for team in document["injuries"]:
        for row in team["injuries"]:
            player_id = _player_id(row)
            if player_id is None:
                unreadable += 1
                continue
            rows.append(
                {
                    "captured_at": captured_at,
                    "player_id": player_id,
                    "team_id": str(team["id"]),
                    "status": str(row.get("status")),
                    "detail": row.get("shortComment"),
                }
            )
    return rows, unreadable


def sync_injuries(data_dir: Path) -> dict[str, Any]:
    """Curate every raw snapshot that has no part yet. Safe to repeat."""
    summary: dict[str, Any] = {
        "new_snapshots": 0,
        "rows": 0,
        "unreadable_rows": 0,
        "bad_snapshots": [],
    }
    quarantine = data_dir / "curated/_rejects/injuries"
    for raw in sorted((data_dir / "raw/espn/injuries").rglob("*.json.gz")):
        stem = raw.name.removesuffix(".json.gz")
        marker = quarantine / f"{stem}.txt"
        if marker.exists():
            continue
        try:
            envelope = json.loads(gzip.decompress(raw.read_bytes()))
            captured_at = datetime.fromisoformat(envelope["captured_at"])
            part = (
                data_dir
                / f"curated/injuries/league=NBA/season={season_of(captured_at)}/part-{stem}.parquet"
            )
            if part.exists():
                continue
            if not envelope["complete"]:
                raise ValueError("incomplete snapshot")
            rows, unreadable = parse_injuries(envelope["responses"][0], captured_at)
        except (KeyError, IndexError, TypeError, ValueError) as error:
            # Reported once, then set aside: one bad file shouldn't fail every later run.
            quarantine.mkdir(parents=True, exist_ok=True)
            marker.write_text(f"{type(error).__name__}: {error}\n", "utf-8")
            summary["bad_snapshots"].append(stem)
            continue
        write_parquet(pl.DataFrame(rows, schema=INJURY_SCHEMA), part)
        summary["new_snapshots"] += 1
        summary["rows"] += len(rows)
        summary["unreadable_rows"] += unreadable
    return summary
