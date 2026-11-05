"""Live ingest (docs/03 section 2.4): new finals, corrections, catch-up, failures and status."""

import json
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import boto3
import httpx
import polars as pl
import pytest
import respx
import yaml
from botocore.exceptions import ClientError
from moto import mock_aws

from hoopsedge.common.dynamo import GameTable, connect, ensure_table
from hoopsedge.common.http import PoliteClient
from hoopsedge.ingest.backfill import SITE, BlockedError, raw_path, write_gz
from hoopsedge.ingest.curated import read_table
from hoopsedge.ingest.live import (
    Live,
    clear_halt,
    ingest_once,
    is_halted,
    run_scheduled,
    scan_days,
    status_report,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "espn"
GAME = "401708333"
DAY = date(2025, 1, 18)
NOW = datetime(2025, 1, 19, 15, 0, tzinfo=UTC)  # 10:00 on 19 January, Eastern.
NCAA_BOARD = f"{SITE}/mens-college-basketball/scoreboard"
NCAA_SUMMARY = f"{SITE}/mens-college-basketball/summary"
NBA_BOARD = f"{SITE}/nba/scoreboard"
INJURIES = f"{SITE}/nba/injuries"


def load(name: str) -> dict[str, Any]:
    document: dict[str, Any] = json.loads((FIXTURES / name).read_text("utf-8"))
    return document


def board_with_game() -> dict[str, Any]:
    board = load("ncaa-20250118-scoreboard.json")
    board["events"] = [e for e in board["events"] if str(e["id"]) == GAME]
    return board


def summary_fixture() -> dict[str, Any]:
    return load("ncaa-20250118-summary.json")


def set_state(board: dict[str, Any], state: str, name: str) -> dict[str, Any]:
    status = board["events"][0]["competitions"][0]["status"]["type"]
    status["state"] = state
    status["name"] = name
    board["events"][0]["status"] = {"type": status}
    return board


class Espn:
    """A fake ESPN: one scoreboard per date, empty for every other date."""

    def __init__(self) -> None:
        self.boards: dict[str, dict[str, Any]] = {"20250118": board_with_game()}
        self.summary: Any = httpx.Response(200, json=summary_fixture())
        respx.get(NBA_BOARD).mock(return_value=httpx.Response(200, json={"events": []}))
        respx.get(NCAA_BOARD).mock(side_effect=self._board)
        self.summary_route = respx.get(NCAA_SUMMARY).mock(side_effect=lambda _: self.summary)
        self.injuries_route = respx.get(INJURIES).mock(
            return_value=httpx.Response(200, json={"injuries": []})
        )

    def _board(self, request: httpx.Request) -> httpx.Response:
        day = request.url.params["dates"]
        return httpx.Response(200, json=self.boards.get(day, {"events": []}))


@pytest.fixture
def table() -> Iterator[GameTable]:
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        ensure_table(resource, "hoopsedge-local")
        yield GameTable(resource.Table("hoopsedge-local"))


def make_live(data_dir: Path, table: GameTable, now: datetime = NOW) -> Live:
    client = PoliteClient("t", rate_per_second=1000.0, max_retries=0, sleep=lambda _: None)
    return Live(data_dir, client, table, now=lambda: now, log=lambda _: None)


@pytest.fixture
def live(tmp_path: Path, table: GameTable) -> Live:
    write_gz(tmp_path / "raw/espn/registry/NBA-teams.json.gz", load("teams-nba.json"))
    return make_live(tmp_path, table)


def games_delta(data_dir: Path) -> Path:
    return data_dir / f"curated/_deltas/games/2025-01-18/NCAA-2025-{GAME}.parquet"


@respx.mock
def test_a_new_final_is_stored_upserted_and_emitted(live: Live, table: GameTable) -> None:
    Espn()
    result = ingest_once(live)
    assert result.new_finals == [GAME]
    assert result.failures == {}
    assert raw_path(live.data_dir, "NCAA", DAY, GAME).exists()

    games = read_table(live.data_dir, "games", "NCAA", 2025)
    assert games["game_id"].to_list() == [GAME]
    assert games["home_pts"].to_list() == [68]
    teams = read_table(live.data_dir, "team_games", "NCAA", 2025)
    assert sorted(teams["team_id"].to_list()) == ["2", "61"]

    item = table.get_game("NCAA", GAME)
    assert item is not None
    assert item["pk"] == f"GAME#NCAA#{GAME}"
    assert item["sk"] == "META"
    assert item["gsi1pk"] == "SLATE#NCAA#2025-01-18"
    assert item["gsi1sk"] == "2025-01-18T18:00:00+00:00"
    assert (item["home_pts"], item["away_pts"]) == (68, 70)
    assert item["status"] == "STATUS_FINAL"


@respx.mock
def test_second_run_makes_no_summary_call_and_emits_nothing(live: Live) -> None:
    espn = Espn()
    ingest_once(live)
    calls = espn.summary_route.call_count
    result = ingest_once(live)
    assert result.new_finals == []
    assert espn.summary_route.call_count == calls


@respx.mock
def test_losing_the_database_does_not_reprocess_games(tmp_path: Path, live: Live) -> None:
    """The local stand-in forgets everything on restart. Files decide what's been processed."""
    espn = Espn()
    ingest_once(live)
    calls = espn.summary_route.call_count
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        ensure_table(resource, "other")
        fresh = GameTable(resource.Table("other"))
        result = ingest_once(make_live(tmp_path, fresh))
        assert fresh.get_game("NCAA", GAME) is not None  # The item comes back.
    assert result.new_finals == []
    assert espn.summary_route.call_count == calls


@respx.mock
def test_a_score_correction_is_flagged_and_replaces_the_row(live: Live, table: GameTable) -> None:
    espn = Espn()
    ingest_once(live)
    corrected = board_with_game()
    home = corrected["events"][0]["competitions"][0]["competitors"][0]
    home["score"] = "69"
    home["linescores"][1]["value"] = 42.0
    espn.boards["20250118"] = corrected
    calls = espn.summary_route.call_count
    result = ingest_once(live)
    assert result.corrections == [GAME]
    assert result.new_finals == [GAME]
    assert espn.summary_route.call_count == calls + 1
    assert read_table(live.data_dir, "games", "NCAA", 2025)["home_pts"].to_list() == [69]
    item = table.get_game("NCAA", GAME)
    assert item is not None
    assert item["home_pts"] == 69


@respx.mock
def test_a_box_that_arrives_late_is_picked_up(live: Live) -> None:
    espn = Espn()
    empty = summary_fixture()
    empty["boxscore"] = {"teams": [], "players": []}
    espn.summary = httpx.Response(200, json=empty)
    first = ingest_once(live)
    assert first.new_finals == [GAME]
    games = read_table(live.data_dir, "games", "NCAA", 2025)
    assert games["box_missing"].to_list() == [True]

    espn.summary = httpx.Response(200, json=summary_fixture())
    second = ingest_once(live)
    assert second.box_updates == [GAME]
    assert second.new_finals == [GAME]  # Ratings need the box too.
    games = read_table(live.data_dir, "games", "NCAA", 2025)
    assert games["box_missing"].to_list() == [False]
    assert len(read_table(live.data_dir, "team_games", "NCAA", 2025)) == 2

    calls = espn.summary_route.call_count
    assert ingest_once(live).new_finals == []
    assert espn.summary_route.call_count == calls


@respx.mock
def test_a_rejected_game_is_quarantined_once_and_not_emitted(live: Live) -> None:
    espn = Espn()
    bad = board_with_game()
    bad["events"][0]["competitions"][0]["competitors"][0]["score"] = "99"
    espn.boards["20250118"] = bad
    result = ingest_once(live)
    assert result.new_finals == []
    assert result.rejected == [GAME]
    assert not games_delta(live.data_dir).exists()
    rejects = list((live.data_dir / "curated/_rejects/games/live").glob("*.jsonl"))
    assert len(rejects) == 1
    assert json.loads(rejects[0].read_text("utf-8"))["reason"] == "periods_do_not_sum_to_score"

    calls = espn.summary_route.call_count
    second = ingest_once(live)
    assert second.rejected == []
    assert espn.summary_route.call_count == calls


@respx.mock
def test_preseason_and_exhibitions_are_ignored(live: Live, table: GameTable) -> None:
    espn = Espn()
    preseason = board_with_game()
    preseason["events"][0]["season"]["type"] = 1
    espn.boards["20250118"] = preseason
    assert ingest_once(live).new_finals == []
    assert not espn.summary_route.called
    assert table.get_game("NCAA", GAME) is None


def upcoming_board() -> dict[str, Any]:
    return set_state(board_with_game(), "pre", "STATUS_SCHEDULED")


@respx.mock
def test_an_upcoming_game_is_upserted_without_a_summary(live: Live, table: GameTable) -> None:
    espn = Espn()
    espn.boards = {"20250119": upcoming_board()}
    result = ingest_once(live)
    assert result.new_finals == []
    assert not espn.summary_route.called
    item = table.get_game("NCAA", GAME)
    assert item is not None
    assert item["status"] == "STATUS_SCHEDULED"
    assert item["venue_id"] == "2088"
    assert item["neutral"] is False
    assert "home_pts" not in item


@respx.mock
def test_an_unchanged_game_is_not_written_again(live: Live, table: GameTable) -> None:
    espn = Espn()
    espn.boards = {"20250119": upcoming_board()}
    ingest_once(live)
    spy = MagicMock(wraps=table.table.put_item)
    table.table.put_item = spy
    ingest_once(live)
    assert not spy.called


@respx.mock
def test_missing_venue_uses_one_summary_call_kept_apart_from_finals(
    live: Live, table: GameTable
) -> None:
    espn = Espn()
    board = upcoming_board()
    competition = board["events"][0]["competitions"][0]
    del competition["venue"]
    espn.boards = {"20250119": board}
    summary = summary_fixture()
    summary["gameInfo"] = {"venue": {"id": "777"}}
    espn.summary = httpx.Response(200, json=summary)
    ingest_once(live)
    assert espn.summary_route.call_count == 1
    item = table.get_game("NCAA", GAME)
    assert item is not None
    assert item["venue_id"] == "777"
    assert not raw_path(live.data_dir, "NCAA", date(2025, 1, 19), GAME).exists()

    ingest_once(live)
    assert espn.summary_route.call_count == 1


def test_scan_runs_from_yesterday_when_earlier_dates_are_settled(live: Live) -> None:
    write_gz(raw_path(live.data_dir, "NCAA", date(2025, 1, 17), "scoreboard"), {"events": []})
    assert scan_days(live, "NCAA") == [date(2025, 1, 18), date(2025, 1, 19), date(2025, 1, 20)]


def test_yesterday_is_rescanned_even_once_settled_so_corrections_show(live: Live) -> None:
    write_gz(raw_path(live.data_dir, "NCAA", date(2025, 1, 18), "scoreboard"), {"events": []})
    assert scan_days(live, "NCAA")[0] == date(2025, 1, 18)


def test_scan_catches_up_after_days_offline(live: Live) -> None:
    write_gz(raw_path(live.data_dir, "NCAA", date(2025, 1, 14), "scoreboard"), {"events": []})
    assert scan_days(live, "NCAA")[0] == date(2025, 1, 15)


def test_scan_with_no_history_looks_back_a_limited_way(live: Live) -> None:
    assert scan_days(live, "NCAA")[0] == date(2025, 1, 5)


@respx.mock
def test_a_finished_date_becomes_the_canonical_scoreboard(live: Live) -> None:
    Espn()
    ingest_once(live)
    assert raw_path(live.data_dir, "NCAA", DAY, "scoreboard").exists()


@respx.mock
def test_a_date_with_a_game_still_playing_is_not_settled(live: Live) -> None:
    espn = Espn()
    espn.boards["20250118"] = set_state(board_with_game(), "in", "STATUS_IN_PROGRESS")
    ingest_once(live)
    assert not raw_path(live.data_dir, "NCAA", DAY, "scoreboard").exists()


@respx.mock
def test_today_is_never_settled(live: Live) -> None:
    espn = Espn()
    espn.boards = {"20250119": board_with_game()}
    ingest_once(live)
    assert not raw_path(live.data_dir, "NCAA", date(2025, 1, 19), "scoreboard").exists()


@respx.mock
def test_every_scan_keeps_a_timestamped_raw_board(live: Live) -> None:
    Espn()
    ingest_once(live)
    files = list((live.data_dir / "raw/espn/live/2025/01/19").glob("NCAA-20250118-*.json.gz"))
    assert len(files) == 1


@respx.mock
def test_a_failed_summary_fails_the_run_and_a_later_run_recovers(live: Live) -> None:
    espn = Espn()
    espn.summary = httpx.Response(500)
    run_scheduled(live)
    runs = [json.loads(line) for line in (live.data_dir / "ingest/runs.jsonl").open()]
    assert runs[-1]["outcome"] == "failed"
    assert GAME in json.dumps(runs[-1]["failures"])
    assert not raw_path(live.data_dir, "NCAA", DAY, "scoreboard").exists()

    espn.summary = httpx.Response(200, json=summary_fixture())
    later = make_live(live.data_dir, live.table, NOW + timedelta(minutes=30))
    run_scheduled(later)
    runs = [json.loads(line) for line in (live.data_dir / "ingest/runs.jsonl").open()]
    assert runs[-1]["outcome"] == "ok"
    assert runs[-1]["new_finals"] == [GAME]
    assert raw_path(live.data_dir, "NCAA", DAY, "scoreboard").exists()


@respx.mock
@pytest.mark.parametrize(
    "response",
    [httpx.Response(403, text="no"), httpx.Response(200, text="<html>challenge</html>")],
    ids=["forbidden", "challenge"],
)
def test_a_block_halts_until_the_owner_clears_it(live: Live, response: httpx.Response) -> None:
    espn = Espn()
    espn.summary = response
    with pytest.raises(BlockedError):
        ingest_once(live)
    assert is_halted(live.data_dir)
    calls = respx.calls.call_count
    with pytest.raises(BlockedError):
        ingest_once(live)
    assert respx.calls.call_count == calls

    clear_halt(live.data_dir)
    espn.summary = httpx.Response(200, json=summary_fixture())
    assert ingest_once(live).new_finals == [GAME]


@respx.mock
def test_a_block_on_injuries_also_halts(live: Live) -> None:
    espn = Espn()
    espn.injuries_route.mock(return_value=httpx.Response(403))
    with pytest.raises(BlockedError):
        ingest_once(live)
    assert is_halted(live.data_dir)


@respx.mock
def test_scheduled_run_records_a_block_and_then_stands_down(live: Live) -> None:
    espn = Espn()
    espn.summary = httpx.Response(403)
    run_scheduled(live)
    later = make_live(live.data_dir, live.table, NOW + timedelta(hours=2))
    calls = respx.calls.call_count
    run_scheduled(later)
    assert respx.calls.call_count == calls
    runs = [json.loads(line) for line in (live.data_dir / "ingest/runs.jsonl").open()]
    assert [run["outcome"] for run in runs] == ["blocked"]


@respx.mock
def test_a_database_failure_is_loud_and_the_next_run_repairs_it(
    live: Live, table: GameTable
) -> None:
    Espn()
    real_put = table.table.put_item
    error = ClientError({"Error": {"Code": "ServiceUnavailable", "Message": "down"}}, "PutItem")
    table.table.put_item = MagicMock(side_effect=error)
    run_scheduled(live)
    runs = [json.loads(line) for line in (live.data_dir / "ingest/runs.jsonl").open()]
    assert runs[-1]["outcome"] == "failed"
    assert games_delta(live.data_dir).exists()

    table.table.put_item = real_put
    later = make_live(live.data_dir, table, NOW + timedelta(minutes=30))
    run_scheduled(later)
    assert table.get_game("NCAA", GAME) is not None
    runs = [json.loads(line) for line in (live.data_dir / "ingest/runs.jsonl").open()]
    assert runs[-1]["outcome"] == "ok"


@respx.mock
def test_explicit_dates_replace_the_normal_scan(live: Live) -> None:
    espn = Espn()
    result = ingest_once(live, dates=[DAY])
    assert result.new_finals == [GAME]
    assert result.scanned["NCAA"] == ["2025-01-18"]
    assert espn.injuries_route.called


@respx.mock
def test_scheduled_run_waits_for_its_interval(live: Live) -> None:
    Espn()
    run_scheduled(live)
    soon = make_live(live.data_dir, live.table, NOW + timedelta(minutes=29))
    run_scheduled(soon)
    due = make_live(live.data_dir, live.table, NOW + timedelta(minutes=30))
    run_scheduled(due)
    lines = (live.data_dir / "ingest/runs.jsonl").read_text("utf-8").splitlines()
    assert len(lines) == 2


@respx.mock
def test_status_reports_health_new_finals_and_staleness(live: Live) -> None:
    assert status_report(live.data_dir, NOW)[1] is False  # Nothing has run.
    Espn()
    run_scheduled(live)
    report, healthy = status_report(live.data_dir, NOW)
    assert healthy is True
    assert "1 new final" in report

    report, healthy = status_report(live.data_dir, NOW + timedelta(hours=3))
    assert healthy is False
    assert "STALE" in report


@respx.mock
def test_a_malformed_game_fails_alone_and_keeps_its_date_open(live: Live) -> None:
    espn = Espn()
    board = board_with_game()
    broken = json.loads(json.dumps(board["events"][0]))
    broken["id"] = "111"
    del broken["competitions"][0]["competitors"][0]["id"]
    board["events"].append(broken)
    espn.boards["20250118"] = board
    result = ingest_once(live)
    assert result.new_finals == [GAME]
    assert "111" in result.failures
    assert not raw_path(live.data_dir, "NCAA", DAY, "scoreboard").exists()


@respx.mock
def test_a_game_without_home_and_away_is_rejected_without_crashing(
    live: Live, table: GameTable
) -> None:
    espn = Espn()
    board = board_with_game()
    board["events"][0]["competitions"][0]["competitors"][0]["homeAway"] = "away"
    espn.boards["20250118"] = board
    result = ingest_once(live)
    assert result.rejected == [GAME]
    assert result.failures == {}
    assert table.get_game("NCAA", GAME) is None


@respx.mock
def test_an_injury_response_with_the_wrong_shape_fails_the_run(live: Live) -> None:
    espn = Espn()
    espn.injuries_route.mock(return_value=httpx.Response(200, json={"teams": []}))
    result = ingest_once(live)
    assert "injuries" in result.failures
    assert result.new_finals == [GAME]


@respx.mock
def test_injury_snapshots_reach_the_curated_table(live: Live) -> None:
    espn = Espn()
    espn.injuries_route.mock(return_value=httpx.Response(200, json=load("nba-injuries.json")))
    result = ingest_once(live)
    assert result.injuries["new_snapshots"] == 1
    assert result.injuries["rows"] > 0
    assert list((live.data_dir / "curated/injuries/league=NBA").rglob("part-*.parquet"))


def test_status_needs_only_files(tmp_path: Path) -> None:
    report, healthy = status_report(tmp_path, NOW)
    assert healthy is False
    assert "Never run" in report


def test_local_never_connects_without_an_explicit_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOOPSEDGE_ENV", "local")
    monkeypatch.delenv("AWS_ENDPOINT_URL_DYNAMODB", raising=False)
    with pytest.raises(RuntimeError, match="ENDPOINT"):
        connect()


def test_compose_points_live_ingest_at_the_stand_in_only() -> None:
    compose_file = Path(__file__).resolve().parents[2] / "docker-compose.yml"
    services = yaml.safe_load(compose_file.read_text("utf-8"))["services"]
    service = services["live-ingest"]
    assert service["environment"]["AWS_ENDPOINT_URL_DYNAMODB"] == "http://aws-mock:5000"
    assert service["environment"]["AWS_ACCESS_KEY_ID"] == "local"
    assert service["volumes"] == ["./data:/workspace/data"]
    assert service["restart"] == "unless-stopped"
    assert "aws-mock" in services
    assert not services["aws-mock"].get("volumes")


def test_read_table_lets_a_delta_replace_a_base_row(tmp_path: Path) -> None:
    from hoopsedge.ingest.curated import write_parquet

    base = tmp_path / "curated/team_games/league=NCAA/season=2025/part-0.parquet"
    rows = [
        {"game_id": "1", "team_id": "a", "pts": 10},
        {"game_id": "1", "team_id": "b", "pts": 20},
        {"game_id": "2", "team_id": "a", "pts": 30},
    ]
    write_parquet(pl.DataFrame(rows), base)
    delta = tmp_path / "curated/_deltas/team_games/2025-01-18/NCAA-2025-1.parquet"
    write_parquet(pl.DataFrame([{"game_id": "1", "team_id": "a", "pts": 11}]), delta)
    other = tmp_path / "curated/_deltas/team_games/2025-01-18/NBA-2025-9.parquet"
    write_parquet(pl.DataFrame([{"game_id": "9", "team_id": "z", "pts": 99}]), other)

    merged = read_table(tmp_path, "team_games", "NCAA", 2025).sort(["game_id", "team_id"])
    assert merged["pts"].to_list() == [11, 30]  # Game 1 comes wholly from its delta.


def test_read_table_with_nothing_is_empty(tmp_path: Path) -> None:
    assert read_table(tmp_path, "games", "NCAA", 2025).is_empty()
