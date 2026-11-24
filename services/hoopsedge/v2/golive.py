"""Go-live readiness (brief §12 phase 8, B7). Read-only: it never books and never changes a switch.

Going live takes three steps, and this says where each one stands:

1. **Three clean practice days.** For each of the last three full Lagos days, every sport had at
   least one scheduled slate run and all of them succeeded, no scheduler job exited with an
   error, and no CRITICAL alert was raised.
2. **One supervised real code per sport,** booked from Run now with the dashboard's Dry run off,
   that read back exactly as intended (status `booked`).
3. **The owner turns scheduled booking on** (`HOOPSEDGE_SCHEDULED_DRY_RUN=false`, D97). Nothing
   here can do that.

    python -m hoopsedge.v2.golive [--days 3]
"""

import argparse
import json
import os
import sys
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from boto3.dynamodb.conditions import Key

from hoopsedge.pipeline.schedule import LAGOS
from hoopsedge.v2.views import REBOOK, SPORTS, runs_root

DAYS = 3
SCHEDULED = "scheduled"
SUCCEEDED = "succeeded"
CRITICAL = "CRITICAL"
BOOKED = "booked"
DEFAULT_DATA_DIR = "/workspace/data"


def _lagos_day(stamp: str) -> date:
    return datetime.fromisoformat(stamp).astimezone(LAGOS).date()


def window(now: datetime, days: int = DAYS) -> list[date]:
    """The last `days` full Lagos days, oldest first. Today isn't over, so it isn't one of them."""
    today = now.astimezone(LAGOS).date()
    return [today - timedelta(days=back) for back in range(days, 0, -1)]


def read_runs(data_dir: Path) -> list[dict[str, Any]]:
    runs = []
    for path in sorted(runs_root(data_dir).glob("*/*/*/run.json")):
        try:
            runs.append(json.loads(path.read_text("utf-8")))
        except (OSError, json.JSONDecodeError):
            continue  # A run being written right now; the next look will see it.
    return runs


def read_jobs(data_dir: Path) -> list[dict[str, Any]]:
    path = data_dir / "scheduler" / "runs.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


def _day_view(
    day: date,
    runs: list[dict[str, Any]],
    jobs: list[dict[str, Any]],
    alerts: list[dict[str, Any]],
) -> dict[str, Any]:
    sports: dict[str, dict[str, Any]] = {}
    problems: list[str] = []
    for sport in SPORTS:
        scheduled = [
            run
            for run in runs
            if run.get("sport") == sport
            and run.get("trigger") == SCHEDULED
            and run.get("started_at")
            and _lagos_day(run["started_at"]) == day
        ]
        failed = [run for run in scheduled if run.get("status") != SUCCEEDED]
        sports[sport] = {
            "runs": len(scheduled),
            "failed": len(failed),
            "tickets": sum(len(run.get("tickets") or []) for run in scheduled),
        }
        if not scheduled:
            problems.append(f"{sport}: no scheduled run")
        for run in failed:
            problems.append(f"{sport}: run {run['run_id']} {run.get('status', 'unknown')}")
    for job in jobs:
        if _lagos_day(job["at"]) == day and job.get("exit") != 0:
            problems.append(f"job {job['job']} exited {job.get('exit')}")
    critical = [
        alert
        for alert in alerts
        if alert.get("severity") == CRITICAL and _lagos_day(alert["sk"].split("#")[0]) == day
    ]
    problems += [f"CRITICAL: {alert.get('message', '')}" for alert in critical]
    return {"day": day.isoformat(), "clean": not problems, "sports": sports, "problems": problems}


def live_codes(runs: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any] | None]:
    """Each sport's first real code that read back exactly as intended (step 2)."""
    found: dict[str, dict[str, Any] | None] = dict.fromkeys(SPORTS)
    for run in sorted(runs, key=lambda run: str(run.get("started_at") or "")):
        sport = run.get("sport")
        if sport not in found or found[sport] is not None or run.get("dry_run", True):
            continue
        for ticket in run.get("tickets") or []:
            if ticket.get("status") == BOOKED and ticket.get("code"):
                found[sport] = {
                    "code": ticket["code"],
                    "run_id": run["run_id"],
                    "slate": run.get("slate"),
                    "booked_at": run.get("started_at"),
                    "rebook": run.get("trigger") == REBOOK,
                }
                break
    return found


def readiness(
    runs: list[dict[str, Any]],
    jobs: list[dict[str, Any]],
    alerts: list[dict[str, Any]],
    now: datetime,
    days: int = DAYS,
) -> dict[str, Any]:
    """Pure. Where each go-live step stands."""
    day_views = [_day_view(day, runs, jobs, alerts) for day in window(now, days)]
    clean_days = all(view["clean"] for view in day_views)
    live = live_codes(runs)
    return {
        "days_needed": days,
        "days": day_views,
        "practice_clean": clean_days,
        "live_codes": live,
        "live_done": all(live.values()),
        "ready": clean_days and all(live.values()),
    }


def alerts_since(table: Any, since: datetime) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    query: dict[str, Any] = {
        "KeyConditionExpression": Key("pk").eq("ALERT") & Key("sk").gte(since.isoformat())
    }
    while True:
        response = table.query(**query)
        items += response["Items"]
        if "LastEvaluatedKey" not in response:
            return items
        query["ExclusiveStartKey"] = response["LastEvaluatedKey"]


def since_for(now: datetime, days: int = DAYS) -> datetime:
    first = window(now, days)[0]
    return datetime.combine(first, datetime.min.time(), tzinfo=LAGOS).astimezone(UTC)


def report_text(result: dict[str, Any]) -> str:
    lines = [f"Practice days (last {result['days_needed']} full Lagos days):"]
    for view in result["days"]:
        counts = ", ".join(
            f"{sport} {s['runs'] - s['failed']}/{s['runs']} runs, {s['tickets']} tickets"
            for sport, s in view["sports"].items()
        )
        lines.append(f"  {view['day']}  {'clean' if view['clean'] else 'NOT CLEAN'}  {counts}")
        lines += [f"      - {problem}" for problem in view["problems"]]
    lines.append("Supervised real codes:")
    for sport, code in result["live_codes"].items():
        text = f"{code['code']} ({code['slate']}, run {code['run_id']})" if code else "none yet"
        lines.append(f"  {sport}: {text}")
    if result["ready"]:
        lines.append("Ready. Scheduled booking is the owner's switch: HOOPSEDGE_SCHEDULED_DRY_RUN.")
    elif result["practice_clean"]:
        lines.append("Practice days are clean. Next: one supervised real code per sport.")
    else:
        lines.append("Not ready: practice days aren't clean yet.")
    return "\n".join(lines)


def main() -> int:
    from hoopsedge.common.dynamo import connect, table_name  # noqa: PLC0415

    parser = argparse.ArgumentParser(prog="hoopsedge.v2.golive", description=__doc__)
    parser.add_argument("--days", type=int, default=DAYS)
    args = parser.parse_args()
    data_dir = Path(os.environ.get("HOOPSEDGE_DATA_DIR", DEFAULT_DATA_DIR))
    now = datetime.now(UTC)
    table = connect().Table(table_name())
    alerts = alerts_since(table, since_for(now, args.days))
    result = readiness(read_runs(data_dir), read_jobs(data_dir), alerts, now, args.days)
    print(report_text(result))
    return 0 if result["practice_clean"] else 1


if __name__ == "__main__":
    sys.exit(main())
