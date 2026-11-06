"""The daily cycle for local soak testing, until EventBridge takes over in Phase 1.

    docker compose --profile manual up -d scheduler

Dry-run slates at D71's times, grading every 30 minutes and learning at 11:00 Lagos (docs/00). Each
job is its own process, so memory goes back between jobs, and one failure can't stop the others. A
file at `data/scheduler/PAUSED` pauses everything, like the `pause_all` flag.
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
RUN_TIMES = {"NCAA": clock(15, 0), "NBA": clock(22, 0)}  # D71.
NBA_LATE_TIME = clock(0, 30)  # D78: later tips get an injury report inside the 3-hour window.
LEARN_TIME = clock(11, 0)
GRADE_EVERY = timedelta(minutes=30)
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

    learn_at = _clock(times.get("nightly_learn"), LEARN_TIME)
    for league in ("NBA", "NCAA"):  # C6: one league at a time.
        learn = _module("hoopsedge.learning.run", "learn", "--league", league)
        daily(f"learn-{league}", learn_at, learn)
    slate = now.astimezone(EASTERN).date().isoformat()
    for league, at in RUN_TIMES.items():
        run = _module(
            "hoopsedge.pipeline.slate", "run", "--league", league, "--date", slate,
            "--trigger", "scheduled",
        )  # fmt: skip
        daily(f"slate-{league}", _clock(times.get(f"{league.lower()}_slate"), at), run)
        if league == "NBA":
            daily("slate-NBA-late", _clock(times.get("nba_late_slate"), NBA_LATE_TIME), run)
    last = done.get("grade")
    if last is None or now - datetime.fromisoformat(last) >= GRADE_EVERY:
        jobs.append(Job("grade", now.isoformat(), _module("hoopsedge.grading.run")))
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
