"""Go-live readiness (B7): three clean practice days, then one verified real code per sport."""

import json
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import boto3
import pytest
from moto import mock_aws

from hoopsedge.common.dynamo import ensure_table
from hoopsedge.v2 import golive
from hoopsedge.v2.store import alert
from hoopsedge.v2.views import SPORTS

NOW = datetime(2026, 11, 28, 10, 0, tzinfo=UTC)  # 11:00 Lagos on the 28th.
DAYS = ["2026-11-25", "2026-11-26", "2026-11-27"]


def run(sport: str, day: str, **extra: Any) -> dict[str, Any]:
    document = {
        "run_id": f"{sport}-{day}-{len(extra)}",
        "sport": sport,
        "slate": day,
        "trigger": "scheduled",
        "status": "succeeded",
        "dry_run": True,
        "started_at": f"{day}T15:00:00+00:00",  # 16:00 Lagos.
        "tickets": [{"status": "dry_run", "code": "DRY-1"}],
    }
    return document | extra


@pytest.fixture
def table() -> Iterator[Any]:
    with mock_aws():
        resource = boto3.resource(
            "dynamodb", region_name="us-east-1",
            aws_access_key_id="x", aws_secret_access_key="x",  # noqa: S106 - Dummy values.
        )  # fmt: skip
        ensure_table(resource, "t")
        yield resource.Table("t")


def clean_runs() -> list[dict[str, Any]]:
    return [run(sport, day) for day in DAYS for sport in SPORTS]


def test_the_window_is_the_last_three_full_lagos_days() -> None:
    assert golive.window(NOW) == [date(2026, 11, 25), date(2026, 11, 26), date(2026, 11, 27)]
    # 23:30 UTC on the 27th is already the 28th in Lagos.
    late = datetime(2026, 11, 27, 23, 30, tzinfo=UTC)
    assert golive.window(late)[-1] == date(2026, 11, 27)


def test_three_clean_days_pass_the_practice_step() -> None:
    result = golive.readiness(clean_runs(), [], [], NOW)
    assert result["practice_clean"] is True
    assert result["ready"] is False  # No real codes yet.
    assert [view["day"] for view in result["days"]] == DAYS
    assert result["days"][0]["sports"]["NCAAB"] == {"runs": 1, "failed": 0, "tickets": 1}


def test_a_failed_run_a_missing_sport_a_failed_job_or_a_critical_alert_spoils_its_day() -> None:
    runs = clean_runs()
    runs[0] = run("NBA", DAYS[0], status="failed")
    runs = [r for r in runs if not (r["sport"] == "NCAAF" and r["slate"] == DAYS[1])]
    jobs = [{"job": "grading", "at": "2026-11-27T08:00:30+00:00", "exit": 1}]
    alerts = [
        {"sk": "2026-11-27T20:00:00+00:00#run-failed", "severity": "CRITICAL", "message": "x"},
        {"sk": "2026-11-26T20:00:00+00:00#note", "severity": "WARNING", "message": "fine"},
    ]
    result = golive.readiness(runs, jobs, alerts, NOW)
    first, second, third = result["days"]
    assert first["problems"] == ["NBA: run NBA-2026-11-25-1 failed"]
    assert second["problems"] == ["NCAAF: no scheduled run"]
    assert third["problems"] == ["job grading exited 1", "CRITICAL: x"]
    assert result["practice_clean"] is False


def test_manual_runs_and_rebooks_dont_count_as_scheduled_days() -> None:
    runs = [r for r in clean_runs() if r["sport"] != "NBA"]
    runs += [run("NBA", day, trigger="dashboard") for day in DAYS]
    assert golive.readiness(runs, [], [], NOW)["practice_clean"] is False


def test_only_a_real_code_that_read_back_right_counts_as_supervised() -> None:
    runs = clean_runs()
    runs += [
        run("NBA", DAYS[2], trigger="dashboard", dry_run=False,
            tickets=[{"status": "invalid", "code": "BAD1"}]),
        run("NBA", DAYS[2], trigger="dashboard", dry_run=False, run_id="nba-live",
            started_at="2026-11-27T16:00:00+00:00",
            tickets=[{"status": "failed", "code": None}, {"status": "booked", "code": "GOOD1"}]),
    ]  # fmt: skip
    live = golive.readiness(runs, [], [], NOW)["live_codes"]
    assert live["NBA"] == {
        "code": "GOOD1",
        "run_id": "nba-live",
        "slate": DAYS[2],
        "booked_at": "2026-11-27T16:00:00+00:00",
        "rebook": False,
    }
    assert live["NCAAB"] is None


def test_ready_needs_clean_days_and_a_real_code_for_every_sport() -> None:
    runs = clean_runs() + [
        run(sport, DAYS[2], trigger="dashboard", dry_run=False, run_id=f"{sport}-live",
            tickets=[{"status": "booked", "code": f"{sport}1"}])
        for sport in SPORTS
    ]  # fmt: skip
    result = golive.readiness(runs, [], [], NOW)
    assert result["live_done"] is True
    assert result["ready"] is True
    assert "Ready." in golive.report_text(result)


def test_runs_jobs_and_alerts_are_read_from_their_files_and_table(tmp_path: Path) -> None:
    folder = tmp_path / "v2/runs/NBA" / DAYS[0] / "01RUN"
    folder.mkdir(parents=True)
    (folder / "run.json").write_text(json.dumps(run("NBA", DAYS[0])), "utf-8")
    (tmp_path / "v2/runs/NBA" / DAYS[0] / "index.json").write_text("{}", "utf-8")
    scheduler = tmp_path / "scheduler"
    scheduler.mkdir()
    (scheduler / "runs.jsonl").write_text('{"job": "backup", "at": "x", "exit": 0}\n\n', "utf-8")
    assert [r["sport"] for r in golive.read_runs(tmp_path)] == ["NBA"]
    assert golive.read_jobs(tmp_path) == [{"job": "backup", "at": "x", "exit": 0}]
    assert golive.read_jobs(tmp_path / "missing") == []


def test_alerts_are_queried_from_the_start_of_the_window(table: Any) -> None:
    alert(table, datetime(2026, 11, 24, 22, 0, tzinfo=UTC), "CRITICAL", "old", "o", "r")
    alert(table, datetime(2026, 11, 24, 23, 30, tzinfo=UTC), "CRITICAL", "new", "n", "r")
    since = golive.since_for(NOW)
    assert since == datetime(2026, 11, 24, 23, 0, tzinfo=UTC)  # Midnight Lagos on the 25th.
    assert [item["message"] for item in golive.alerts_since(table, since)] == ["n"]
