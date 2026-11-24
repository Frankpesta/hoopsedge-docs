"""Early data collector (D13): snapshots, failure handling, halting and scheduling."""

import copy
import gzip
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
import yaml

from hoopsedge.common.http import PoliteClient
from hoopsedge.ingest.collector import (
    ESPN_CORE,
    ESPN_SITE,
    SPORTYBET_BASE,
    Context,
    build_jobs,
    collect_injuries,
    collect_lines,
    collect_offers,
    et_date,
    halted_sources,
    read_runs,
    resume_source,
    run_due,
    status_report,
    write_snapshot,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "sportybet"
NOW = datetime(2026, 11, 6, 15, 0, 30, tzinfo=UTC)
OFFERS_URL = f"{SPORTYBET_BASE}/factsCenter/pcUpcomingEvents"
INJURIES_URL = f"{ESPN_SITE}/nba/injuries"
NBA_BOARD_URL = f"{ESPN_SITE}/nba/scoreboard"
NCAA_BOARD_URL = f"{ESPN_SITE}/mens-college-basketball/scoreboard"


def offers_fixture() -> dict[str, Any]:
    document: dict[str, Any] = json.loads((FIXTURES / "events-ncaa-page1.json").read_text("utf-8"))
    count = sum(len(t["events"]) for t in document["data"]["tournaments"])
    document["data"]["totalNum"] = count
    return document


@pytest.fixture
def context(tmp_path: Path) -> Iterator[Context]:
    def client() -> PoliteClient:
        return PoliteClient("test", rate_per_second=1000.0, max_retries=0, sleep=lambda _: None)

    built = Context(tmp_path, client(), client(), now=lambda: NOW)
    yield built
    built.sportybet.close()
    built.espn.close()


def read_gz(path: Path) -> dict[str, Any]:
    document: dict[str, Any] = json.loads(gzip.decompress(path.read_bytes()))
    return document


def snapshots(root: Path) -> list[Path]:
    return sorted(root.rglob("*.json.gz"))


def mock_all_ok() -> None:
    respx.get(OFFERS_URL).mock(return_value=httpx.Response(200, json=offers_fixture()))
    respx.get(INJURIES_URL).mock(return_value=httpx.Response(200, json={"injuries": []}))
    respx.get(NBA_BOARD_URL).mock(return_value=httpx.Response(200, json={"events": []}))
    respx.get(NCAA_BOARD_URL).mock(return_value=httpx.Response(200, json={"events": []}))


def test_et_date_uses_us_eastern_not_utc() -> None:
    assert et_date(datetime(2026, 11, 6, 4, 59, tzinfo=UTC)).isoformat() == "2026-11-05"
    assert et_date(datetime(2026, 11, 6, 5, 0, tzinfo=UTC)).isoformat() == "2026-11-06"


def test_snapshot_records_capture_time_and_leaves_no_temp_file(context: Context) -> None:
    path = write_snapshot(
        context, "raw/espn/injuries", "injuries", [{"a": 1}], NOW - timedelta(seconds=5), True
    )
    assert path.parent == context.data_dir / "raw/espn/injuries/2026/11/06"
    assert path.name == "injuries-20261106T150030Z.json.gz"
    document = read_gz(path)
    assert document["captured_at"] == "2026-11-06T15:00:30+00:00"
    assert document["started_at"] == "2026-11-06T15:00:25+00:00"
    assert document["complete"] is True
    assert document["responses"] == [{"a": 1}]
    assert [p.name for p in path.parent.iterdir()] == [path.name]


@respx.mock
def test_injuries_are_stored_with_a_row_count(context: Context) -> None:
    body = {"injuries": [{"injuries": [{"status": "Out"}, {"status": "Day-To-Day"}]}]}
    respx.get(INJURIES_URL).mock(return_value=httpx.Response(200, json=body))
    assert collect_injuries(context) == {"teams": 1, "rows": 2}
    stored = read_gz(snapshots(context.data_dir / "raw/espn/injuries")[0])
    assert stored["responses"] == [body]


@respx.mock
def test_offers_store_one_file_per_league_with_coverage(context: Context) -> None:
    respx.get(OFFERS_URL).mock(return_value=httpx.Response(200, json=offers_fixture()))
    summary = collect_offers(context)
    files = snapshots(context.data_dir / "raw/sportybet/collector-20261106")
    assert [p.name.split("-2026")[0] for p in files] == ["offers-NBA", "offers-NCAA"]
    assert summary["NCAA"]["events"] == 5
    assert sum(summary["NCAA"]["by_et_date"].values()) == 5
    assert set(summary["NCAA"]["bookable"]) == {"223", "225", "68", "227", "228"}


@respx.mock
def test_offers_follow_every_page(context: Context) -> None:
    first = offers_fixture()
    second = copy.deepcopy(first)
    first["data"]["totalNum"] = second["data"]["totalNum"] = 10
    route = respx.get(OFFERS_URL).mock(
        side_effect=[
            httpx.Response(200, json=first),
            httpx.Response(200, json=second),
            httpx.Response(200, json=first),
            httpx.Response(200, json=second),
        ]
    )
    summary = collect_offers(context)
    assert route.call_count == 4
    assert summary["NBA"]["events"] == summary["NCAA"]["events"] == 10
    assert route.calls[1].request.url.params["pageNum"] == "2"


@respx.mock
def test_failed_biz_code_fails_the_job_but_keeps_the_response(context: Context) -> None:
    body = {"bizCode": 19000, "message": "nope"}
    respx.get(OFFERS_URL).mock(return_value=httpx.Response(200, json=body))
    job = next(j for j in build_jobs() if j.name == "sportybet_offers")
    run_due([job], context)
    run = read_runs(context.data_dir)[-1]
    assert run["outcome"] == "failed"
    assert "19000" in run["error"]
    stored = read_gz(snapshots(context.data_dir / "raw/sportybet")[0])
    assert stored["complete"] is False
    assert stored["responses"] == [body]


@respx.mock
def test_empty_injuries_shape_change_is_a_failure(context: Context) -> None:
    respx.get(INJURIES_URL).mock(return_value=httpx.Response(200, json={"teams": []}))
    job = next(j for j in build_jobs() if j.name == "nba_injuries")
    run_due([job], context)
    assert read_runs(context.data_dir)[-1]["outcome"] == "failed"


@respx.mock
def test_lines_fetch_odds_only_for_games_not_started(context: Context) -> None:
    def game(game_id: str, state: str) -> dict[str, Any]:
        return {"id": game_id, "status": {"type": {"state": state}}}

    board = {"events": [game("1", "pre"), game("2", "in"), game("3", "post"), game("4", "pre")]}
    respx.get(NBA_BOARD_URL).mock(return_value=httpx.Response(200, json=board))
    respx.get(NCAA_BOARD_URL).mock(return_value=httpx.Response(200, json={"events": []}))
    odds_one = respx.get(f"{ESPN_CORE}/nba/events/1/competitions/1/odds").mock(
        return_value=httpx.Response(200, json={"items": [{"provider": {"name": "x"}}]})
    )
    odds_four = respx.get(f"{ESPN_CORE}/nba/events/4/competitions/4/odds").mock(
        return_value=httpx.Response(404, json={})
    )
    summary = collect_lines(context)
    assert odds_one.called and odds_four.called
    assert summary["NBA"] == {"games": 4, "pre_game": 2, "with_odds": 1}
    assert summary["NCAA"] == {"games": 0, "pre_game": 0, "with_odds": 0}
    assert respx.calls[0].request.url.params["dates"] == "20261106"
    files = [p.name for p in snapshots(context.data_dir / "raw/espn/lines")]
    assert any(name.startswith("NBA-odds-") for name in files)
    assert any(name.startswith("NBA-scoreboard-") for name in files)


@respx.mock
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="<html>Just a moment...</html>"),
        httpx.Response(403, text="Forbidden"),
    ],
    ids=["challenge-page", "forbidden"],
)
def test_block_halts_the_source_until_the_owner_resumes(
    context: Context, response: httpx.Response
) -> None:
    route = respx.get(OFFERS_URL).mock(return_value=response)
    job = next(j for j in build_jobs() if j.name == "sportybet_offers")
    run_due([job], context)
    assert read_runs(context.data_dir)[-1]["outcome"] == "blocked"
    assert halted_sources(read_runs(context.data_dir)) == {"sportybet"}

    calls = route.call_count
    later = Context(
        context.data_dir,
        context.sportybet,
        context.espn,
        now=lambda: NOW + timedelta(days=1),
    )
    run_due([job], later)
    assert route.call_count == calls

    resume_source(later, "sportybet")
    assert halted_sources(read_runs(context.data_dir)) == set()
    route.mock(return_value=httpx.Response(200, json=offers_fixture()))
    run_due([job], later)
    assert route.call_count > calls
    assert read_runs(context.data_dir)[-1]["outcome"] == "ok"


@respx.mock
def test_server_error_fails_one_job_and_others_still_run(context: Context) -> None:
    mock_all_ok()
    respx.get(OFFERS_URL).mock(return_value=httpx.Response(503))
    run_due(build_jobs(), context)
    outcomes = {run["job"]: run["outcome"] for run in read_runs(context.data_dir)}
    assert outcomes == {"sportybet_offers": "failed", "nba_injuries": "ok", "espn_lines": "ok"}
    assert halted_sources(read_runs(context.data_dir)) == set()


@respx.mock
def test_jobs_run_only_when_due_even_after_a_restart(context: Context) -> None:
    mock_all_ok()
    jobs = build_jobs()
    run_due(jobs, context)
    assert len(read_runs(context.data_dir)) == 3

    def at(minutes: int) -> Context:
        return Context(
            context.data_dir,
            context.sportybet,
            context.espn,
            now=lambda: NOW + timedelta(minutes=minutes),
        )

    run_due(jobs, at(29))
    assert len(read_runs(context.data_dir)) == 3
    run_due(jobs, at(30))
    assert [run["job"] for run in read_runs(context.data_dir)][3:] == ["sportybet_offers"]
    run_due(jobs, at(60))
    assert sorted(run["job"] for run in read_runs(context.data_dir)[4:]) == [
        "nba_injuries",
        "sportybet_offers",
    ]


def test_a_torn_last_log_line_is_skipped(context: Context) -> None:
    log = context.data_dir / "collector" / "runs.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text('{"job":"a","source":"s","outcome":"ok","at":"x"}\n{"job":"b","so', "utf-8")
    assert [run["job"] for run in read_runs(context.data_dir)] == ["a"]


def test_collector_only_ever_reads() -> None:
    source = (Path(__file__).parent.parent / "hoopsedge/ingest/collector.py").read_text("utf-8")
    for forbidden in (".post(", ".put(", ".delete(", ".patch(", "share_create", "orders/"):
        assert forbidden not in source


def test_compose_service_survives_branch_switches_and_has_no_aws_access() -> None:
    compose_file = Path(__file__).resolve().parents[2] / "docker-compose.yml"
    service = yaml.safe_load(compose_file.read_text("utf-8"))["services"]["collector"]
    assert service["volumes"] == ["./data:/workspace/data"]  # Code comes from the image.
    assert service["build"]["target"] == "collector"
    assert service["restart"] == "unless-stopped"
    assert service["mem_limit"] == "256m"
    assert not [name for name in service["environment"] if name.startswith("AWS_")]


@respx.mock
def test_status_reports_health_and_listing_growth(context: Context) -> None:
    mock_all_ok()
    run_due(build_jobs(), context)
    report, healthy = status_report(context)
    assert healthy is True
    for expected in ("sportybet_offers", "nba_injuries", "espn_lines", "NCAA"):
        assert expected in report


@respx.mock
def test_status_is_unhealthy_when_a_source_is_halted_or_stale(context: Context) -> None:
    assert status_report(context)[1] is False  # Nothing has run yet.
    respx.get(INJURIES_URL).mock(return_value=httpx.Response(403))
    job = next(j for j in build_jobs() if j.name == "nba_injuries")
    run_due([job], context)
    report, healthy = status_report(context)
    assert healthy is False
    assert "HALTED" in report
