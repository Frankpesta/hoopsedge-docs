"""The v2 daily cycle (brief §12), run on the server until a managed scheduler takes over.

    docker compose --profile manual up -d scheduler

Lagos times, each one overridable from the settings document's `schedules`:

- 05:00 database export (D84, D91).
- 08:00 ESPN scores refresh for every sport, after nearly all US games have ended.
- NCAAF slates at 18:00 every day (weeknight games) and also 14:00 on Saturdays.
- NCAAB and NBA slates at 16:00 and 21:00; the second catches late lines.

A slate run refreshes ESPN itself before predicting, and is dry-run only for now (D95). The slate
date is the US Eastern date when the job starts. Each job is its own process, so memory goes back
between jobs and one failure can't stop the others. A file at `data/scheduler/PAUSED` pauses
everything, like the `pause_all` flag. The v1 jobs (learning, v1 slates, v1 grading) went to
`legacy/` with D96.
"""

import json
import os
import subprocess  # noqa: S404 - Runs this package's own commands, with fixed arguments.
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from datetime import time as clock
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from botocore.exceptions import BotoCoreError, ClientError

from hoopsedge.common.dynamo import connect, table_name
from hoopsedge.ingest.espn_coverage import EASTERN

LAGOS = ZoneInfo("Africa/Lagos")
SPORTS = ("NCAAF", "NCAAB", "NBA")
SATURDAY = 5
BACKUP_TIME = clock(5, 0)
SCORES_TIME = clock(8, 0)
# Name: (sport, default Lagos time, Saturdays only).
SLATES: dict[str, tuple[str, clock, bool]] = {
    "ncaaf_slate": ("NCAAF", clock(18, 0), False),
    "ncaaf_saturday_slate": ("NCAAF", clock(14, 0), True),
    "ncaab_slate": ("NCAAB", clock(16, 0), False),
    "ncaab_late_slate": ("NCAAB", clock(21, 0), False),
    "nba_slate": ("NBA", clock(16, 0), False),
    "nba_late_slate": ("NBA", clock(21, 0), False),
}
TOO_LATE_AFTER = timedelta(hours=3)  # A machine that wakes much later skips that day's job.
POLL_SECONDS = 30
DEFAULT_DATA_DIR = "/workspace/data"


@dataclass(frozen=True)
class Job:
    name: str
    key: str  # Marks it as done, so a restart can't run it twice.
    command: tuple[str, ...]


def _module(*arguments: str) -> tuple[str, ...]:
    return (sys.executable, "-m", *arguments)


def _clock(text: Any, fallback: clock) -> clock:
    return clock.fromisoformat(str(text)) if text else fallback


def due_jobs(
    now: datetime, done: dict[str, str], schedules: dict[str, Any] | None = None
) -> list[Job]:
    """Pure. What should start now, given what's already been done and the times in force."""
    local = now.astimezone(LAGOS)
    day = local.date().isoformat()
    jobs: list[Job] = []
    times = schedules or {}

    def daily(name: str, at: clock, command: tuple[str, ...]) -> None:
        start = datetime.combine(local.date(), at, tzinfo=LAGOS)
        if start <= now < start + TOO_LATE_AFTER and done.get(name) != day:
            jobs.append(Job(name, day, command))

    daily(
        "backup",
        _clock(times.get("backup"), BACKUP_TIME),
        _module("hoopsedge.common.backup", "export"),
    )
    for sport in SPORTS:
        scores = _module("hoopsedge.ingest.scores", "refresh", "--sport", sport)
        daily(f"scores-{sport}", _clock(times.get(f"{sport.lower()}_scores"), SCORES_TIME), scores)
    slate = now.astimezone(EASTERN).date().isoformat()
    for name, (sport, at, saturdays) in SLATES.items():
        if saturdays and local.weekday() != SATURDAY:
            continue
        run = _module("hoopsedge.v2.slate", "run", "--sport", sport, "--date", slate)
        daily(name, _clock(times.get(name), at), run)
    return jobs


def _read_state(path: Path) -> dict[str, str]:
    state: dict[str, str] = json.loads(path.read_text("utf-8")) if path.exists() else {}
    return state


def _write_state(path: Path, state: dict[str, str]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(state, indent=1), "utf-8")
    temporary.replace(path)


def run_due(
    folder: Path,
    now: datetime,
    runner: Any = subprocess.run,
    settings: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Runs what's due, one at a time. A failed job is logged and isn't retried until next due.

    C19: `pause_all` stops everything here. Run now from the dashboard still works.
    """
    if (folder / "PAUSED").exists() or (settings or {}).get("pause_all"):
        return []
    path = folder / "state.json"
    done = _read_state(path)
    results: list[dict[str, Any]] = []
    for job in due_jobs(now, done, (settings or {}).get("schedules")):
        started = time.monotonic()
        finished = runner(list(job.command), check=False, env=os.environ.copy())  # noqa: S603
        result = {
            "job": job.name,
            "at": now.isoformat(),
            "exit": finished.returncode,
            "seconds": round(time.monotonic() - started, 1),
        }
        results.append(result)
        with (folder / "runs.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(result) + "\n")
        done[job.name] = job.key
        _write_state(path, done)
    return results


def main() -> int:
    data_dir = Path(os.environ.get("HOOPSEDGE_DATA_DIR", DEFAULT_DATA_DIR))
    folder = data_dir / "scheduler"
    folder.mkdir(parents=True, exist_ok=True)
    table = connect().Table(table_name())

    def settings() -> dict[str, Any] | None:
        try:
            item = table.get_item(Key={"pk": "CONFIG", "sk": "CURRENT"}).get("Item")
        except (BotoCoreError, ClientError) as error:
            print(f"Can't read settings, so using the defaults: {error}", flush=True)
            return None
        return None if item is None else item.get("document")

    while True:
        run_due(folder, datetime.now(UTC), settings=settings())
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
