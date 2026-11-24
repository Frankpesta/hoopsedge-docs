"""The v2 scheduler (brief §12): Lagos times, the Eastern slate date, pause and moved times."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from hoopsedge.pipeline.schedule import due_jobs, run_due


def _names(now: datetime, done: dict[str, str] | None = None, **times: str) -> set[str]:
    return {job.name for job in due_jobs(now, done or {}, times or None)}


def test_basketball_slates_run_at_16_and_21_lagos() -> None:
    assert _names(datetime(2026, 11, 24, 15, 0, tzinfo=UTC)) == {"ncaab_slate", "nba_slate"}
    assert _names(datetime(2026, 11, 24, 20, 0, tzinfo=UTC)) == {
        "ncaab_late_slate", "nba_late_slate",
    }  # fmt: skip


def test_ncaaf_runs_at_18_daily_and_also_14_on_saturdays() -> None:
    tuesday_14, saturday_14 = (datetime(2026, 11, d, 13, 0, tzinfo=UTC) for d in (24, 28))
    assert "ncaaf_saturday_slate" not in _names(tuesday_14)
    assert "ncaaf_saturday_slate" in _names(saturday_14)
    assert "ncaaf_slate" in _names(datetime(2026, 11, 24, 17, 0, tzinfo=UTC))


def test_a_slate_job_runs_the_v2_slate_for_the_eastern_date() -> None:
    late = datetime(2026, 11, 25, 3, 30, tzinfo=UTC)  # 04:30 Lagos, still the 24th in the US.
    [job] = due_jobs(late, {}, {"nba_late_slate": "04:00"})
    assert job.command[-8:] == (
        "hoopsedge.v2.slate", "run", "--sport", "NBA", "--date", "2026-11-24", "--trigger",
        "scheduled",
    )  # fmt: skip


def test_v1_jobs_are_gone() -> None:
    every = {
        job.name
        for hour in range(24)
        for job in due_jobs(datetime(2026, 11, 28, hour, 0, tzinfo=UTC), {})
    }
    assert not any(name.startswith(("learn", "grade", "slate-")) for name in every)


def test_pause_all_stops_scheduled_jobs_and_a_new_time_moves_them(tmp_path: Path) -> None:
    now = datetime(2026, 11, 24, 15, 0, tzinfo=UTC)
    assert run_due(tmp_path, now, lambda *_, **__: None, {"pause_all": True}) == []
    assert _names(now, nba_slate="17:00") == {"ncaab_slate"}
    assert _names(now + timedelta(hours=1), nba_slate="17:00") == {
        "ncaab_slate", "nba_slate",
    }  # fmt: skip
