"""Backfill: seasons, resumable fetching, block handling, curated tables and coverage."""

import gzip
import json
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import polars as pl
import pytest
import respx

from hoopsedge.common.http import PoliteClient
from hoopsedge.ingest.backfill import (
    CORE,
    FIRST_SEASON,
    SITE,
    Backfill,
    BlockedError,
    build_curated,
    fetch_registry,
    fetch_season,
    raw_path,
    read_gz,
    season_dates,
    seasons_for,
    write_gz,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "espn"
DAY = date(2025, 1, 18)
GAME = "401708333"
SITE_NCAA = f"{SITE}/mens-college-basketball"
SITE_NBA = f"{SITE}/nba"
ODDS_URL = f"{CORE}/mens-college-basketball/events/{GAME}/competitions/{GAME}/odds"


def load(name: str) -> dict[str, Any]:
    document: dict[str, Any] = json.loads((FIXTURES / name).read_text("utf-8"))
    return document


def one_game_board() -> dict[str, Any]:
    board = load("ncaa-20250118-scoreboard.json")
    board["events"] = [e for e in board["events"] if str(e["id"]) == GAME]
    return board


@pytest.fixture
def backfill(tmp_path: Path) -> Backfill:
    client = PoliteClient("t", rate_per_second=1000.0, max_retries=0, sleep=lambda _: None)
    return Backfill(tmp_path, client, today_et=date(2026, 11, 5), log=lambda _: None)


def mock_day(summary: dict[str, Any] | None = None) -> None:
    respx.get(f"{SITE_NCAA}/scoreboard").mock(
        return_value=httpx.Response(200, json=one_game_board())
    )
    respx.get(f"{SITE_NCAA}/summary").mock(
        return_value=httpx.Response(200, json=summary or load("ncaa-20250118-summary.json"))
    )
    respx.get(ODDS_URL).mock(return_value=httpx.Response(200, json={"items": []}))


def test_nba_starts_at_2018_19_and_ncaa_at_2015_16() -> None:
    assert FIRST_SEASON == {"NBA": 2019, "NCAA": 2016}
    assert seasons_for("NBA", date(2026, 11, 5))[0] == 2027  # Newest first.
    assert seasons_for("NBA", date(2026, 11, 5))[-1] == 2019
    assert seasons_for("NCAA", date(2026, 11, 5))[-1] == 2016
    assert seasons_for("NCAA", date(2026, 8, 1))[0] == 2026  # Off-season: last season is newest.


def test_season_dates_cover_covid_shifts_and_stop_before_today() -> None:
    today = date(2026, 11, 5)
    bubble = season_dates("NBA", 2020, today)
    assert bubble[0] <= date(2019, 10, 22) and bubble[-1] >= date(2020, 10, 11)
    late_start = season_dates("NBA", 2021, today)
    assert late_start[0] <= date(2020, 12, 22) and late_start[-1] >= date(2021, 7, 20)
    assert season_dates("NCAA", 2016, today)[0] <= date(2015, 11, 13)
    assert season_dates("NCAA", 2016, today)[-1] >= date(2016, 4, 4)
    assert season_dates("NCAA", 2027, today)[-1] == date(2026, 11, 3)  # Live ingest does the rest.
    assert date(2026, 11, 4) not in season_dates("NBA", 2027, today)


def test_gz_round_trip_is_atomic(tmp_path: Path) -> None:
    path = raw_path(tmp_path, "NCAA", DAY, "scoreboard")
    assert path.as_posix().endswith("raw/espn/NCAA/2025/01/18/scoreboard.json.gz")
    write_gz(path, {"a": 1})
    assert read_gz(path) == {"a": 1}
    assert gzip.decompress(path.read_bytes()) == b'{"a": 1}'
    assert [p.name for p in path.parent.iterdir()] == ["scoreboard.json.gz"]


@respx.mock
def test_games_stage_stores_raw_and_makes_no_odds_calls(backfill: Backfill) -> None:
    mock_day()
    assert fetch_season(backfill, "NCAA", 2025, [DAY]) is True
    folder = backfill.data_dir / "raw/espn/NCAA/2025/01/18"
    assert sorted(p.name for p in folder.iterdir()) == [f"{GAME}.json.gz", "scoreboard.json.gz"]
    assert not respx.routes[2].called


@respx.mock
def test_odds_stage_stores_odds_and_uses_its_own_checkpoint(backfill: Backfill) -> None:
    mock_day()
    fetch_season(backfill, "NCAA", 2025, [DAY])
    assert fetch_season(backfill, "NCAA", 2025, [DAY], "odds") is True
    assert raw_path(backfill.data_dir, "NCAA", DAY, f"{GAME}.odds").exists()
    assert (backfill.data_dir / "backfill/checkpoints/NCAA-2025-odds.json").exists()
    calls = respx.calls.call_count
    assert fetch_season(backfill, "NCAA", 2025, [DAY], "odds") is True
    assert respx.calls.call_count == calls


@respx.mock
def test_odds_stage_leaves_a_date_open_until_its_scoreboard_exists(backfill: Backfill) -> None:
    mock_day()
    assert fetch_season(backfill, "NCAA", 2025, [DAY], "odds") is False
    assert not respx.routes[2].called


@respx.mock
def test_no_odds_at_espn_is_recorded_not_a_failure(backfill: Backfill) -> None:
    mock_day()
    respx.get(ODDS_URL).mock(return_value=httpx.Response(404, json={}))
    fetch_season(backfill, "NCAA", 2025, [DAY])
    assert fetch_season(backfill, "NCAA", 2025, [DAY], "odds") is True
    stored = read_gz(raw_path(backfill.data_dir, "NCAA", DAY, f"{GAME}.odds"))
    assert stored == {"items": [], "not_found": True}


@respx.mock
def test_odds_stage_skips_games_against_non_d1_teams(backfill: Backfill) -> None:
    mock_day()
    fetch_season(backfill, "NCAA", 2025, [DAY])
    write_gz(
        backfill.data_dir / "raw/espn/registry/NCAA-d1-2025.json.gz",
        {"items": [{"$ref": "http://x/seasons/2025/teams/61?lang=en"}]},  # Auburn isn't on it.
    )
    assert fetch_season(backfill, "NCAA", 2025, [DAY], "odds") is True
    assert not respx.routes[2].called


@respx.mock
def test_odds_stage_keeps_games_when_both_teams_are_d1(backfill: Backfill) -> None:
    mock_day()
    fetch_season(backfill, "NCAA", 2025, [DAY])
    refs = [{"$ref": f"http://x/seasons/2025/teams/{team}?lang=en"} for team in ("61", "2")]
    write_gz(backfill.data_dir / "raw/espn/registry/NCAA-d1-2025.json.gz", {"items": refs})
    assert fetch_season(backfill, "NCAA", 2025, [DAY], "odds") is True
    assert respx.routes[2].called


@respx.mock
def test_preseason_exhibition_and_unfinished_games_get_no_summary(backfill: Backfill) -> None:
    def variant(game_id: str, **changes: Any) -> dict[str, Any]:
        event: dict[str, Any] = json.loads(json.dumps(one_game_board()["events"][0]))
        event["id"] = game_id
        if "season_type" in changes:
            event["season"]["type"] = changes["season_type"]
        if "status" in changes:
            event["competitions"][0]["status"]["type"]["name"] = changes["status"]
        if "type" in changes:
            event["competitions"][0]["type"]["abbreviation"] = changes["type"]
        return event

    board = one_game_board()
    board["events"] = [
        variant("1", season_type=1),
        variant("2", type="ALLSTAR"),
        variant("3", status="STATUS_POSTPONED"),
    ]
    respx.get(f"{SITE_NCAA}/scoreboard").mock(return_value=httpx.Response(200, json=board))
    summary_route = respx.get(f"{SITE_NCAA}/summary").mock(return_value=httpx.Response(500))
    assert fetch_season(backfill, "NCAA", 2025, [DAY]) is True
    assert not summary_route.called


@respx.mock
def test_resume_skips_finished_dates_without_any_request(backfill: Backfill) -> None:
    mock_day()
    fetch_season(backfill, "NCAA", 2025, [DAY])
    calls = respx.calls.call_count
    assert fetch_season(backfill, "NCAA", 2025, [DAY]) is True
    assert respx.calls.call_count == calls


@respx.mock
def test_resume_mid_date_does_not_refetch_what_it_has(backfill: Backfill) -> None:
    mock_day()
    write_gz(
        raw_path(backfill.data_dir, "NCAA", DAY, GAME),
        load("ncaa-20250118-summary.json"),
    )
    summary_route = respx.routes[1]
    fetch_season(backfill, "NCAA", 2025, [DAY])
    assert not summary_route.called


@respx.mock
def test_a_failed_game_leaves_the_date_open_and_a_later_run_finishes_it(
    backfill: Backfill,
) -> None:
    mock_day()
    summary_route = respx.get(f"{SITE_NCAA}/summary").mock(return_value=httpx.Response(500))
    assert fetch_season(backfill, "NCAA", 2025, [DAY]) is False
    checkpoint = json.loads(
        (backfill.data_dir / "backfill/checkpoints/NCAA-2025.json").read_text("utf-8")
    )
    assert checkpoint["completed_dates"] == []
    assert "2025-01-18" in checkpoint["failures"]
    assert GAME in checkpoint["failures"]["2025-01-18"]

    summary_route.mock(return_value=httpx.Response(200, json=load("ncaa-20250118-summary.json")))
    assert fetch_season(backfill, "NCAA", 2025, [DAY]) is True
    checkpoint = json.loads(
        (backfill.data_dir / "backfill/checkpoints/NCAA-2025.json").read_text("utf-8")
    )
    assert checkpoint["completed_dates"] == ["2025-01-18"]
    assert checkpoint["failures"] == {}


@respx.mock
@pytest.mark.parametrize(
    "response",
    [httpx.Response(403, text="no"), httpx.Response(200, text="<html>challenge</html>")],
    ids=["forbidden", "challenge"],
)
def test_a_block_stops_everything_and_stays_stopped(
    backfill: Backfill, response: httpx.Response
) -> None:
    route = respx.get(f"{SITE_NCAA}/scoreboard").mock(return_value=response)
    with pytest.raises(BlockedError):
        fetch_season(backfill, "NCAA", 2025, [DAY])
    calls = route.call_count
    with pytest.raises(BlockedError, match="halted"):
        fetch_season(backfill, "NCAA", 2025, [DAY])
    assert route.call_count == calls


@respx.mock
def test_registry_fetch_stores_lists_for_every_season(backfill: Backfill) -> None:
    respx.get(f"{SITE_NBA}/teams").mock(
        return_value=httpx.Response(200, json=load("teams-nba.json"))
    )
    respx.get(f"{SITE_NCAA}/teams").mock(
        return_value=httpx.Response(200, json=load("teams-ncaa.json"))
    )
    core = respx.get(
        url__regex=rf"{CORE}/mens-college-basketball/seasons/\d+/types/2/groups/50/teams"
    ).mock(return_value=httpx.Response(200, json=load("d1-teams-2027.json")))
    fetch_registry(backfill)
    assert core.call_count == 12  # 2015-16 through 2026-27.
    folder = backfill.data_dir / "raw/espn/registry"
    assert (folder / "NBA-teams.json.gz").exists()
    assert (folder / "NCAA-d1-2016.json.gz").exists()
    calls = respx.calls.call_count
    fetch_registry(backfill)
    assert respx.calls.call_count == calls


def seed_raw(data_dir: Path) -> None:
    write_gz(raw_path(data_dir, "NCAA", DAY, "scoreboard"), one_game_board())
    write_gz(raw_path(data_dir, "NCAA", DAY, GAME), load("ncaa-20250118-summary.json"))
    registry = data_dir / "raw/espn/registry"
    write_gz(registry / "NBA-teams.json.gz", load("teams-nba.json"))
    write_gz(registry / "NCAA-teams.json.gz", load("teams-ncaa.json"))
    write_gz(registry / "NCAA-d1-2025.json.gz", load("d1-teams-2027.json"))


def test_build_writes_partitioned_tables_from_raw_alone(tmp_path: Path) -> None:
    seed_raw(tmp_path)
    report_dir = tmp_path / "reports"
    build_curated(tmp_path, report_dir, today_et=date(2026, 11, 5), log=lambda _: None)

    games = pl.read_parquet(tmp_path / "curated/games/league=NCAA/season=2025/part-0.parquet")
    assert games["game_id"].to_list() == [GAME]
    assert games["home_pts"].to_list() == [68]
    assert games["home_periods"].to_list() == [[27, 41]]
    assert games.schema["tip_utc"] == pl.Datetime("us", "UTC")
    assert games.schema["slate_date_et"] == pl.Date

    teams = pl.read_parquet(tmp_path / "curated/team_games/league=NCAA/season=2025/part-0.parquet")
    assert sorted(teams["team_id"].to_list()) == ["2", "61"]
    assert not (tmp_path / "curated/player_games/league=NCAA").exists()

    registry = pl.read_parquet(tmp_path / "curated/teams/teams.parquet")
    assert len(registry.filter(pl.col("league") == "NBA")) == 30
    seasons = pl.read_parquet(tmp_path / "curated/team_seasons/team_seasons.parquet")
    row = seasons.filter((pl.col("season") == 2025) & (pl.col("team_id") == "2"))
    assert row["is_d1"].to_list() == [True]
    aliases = pl.read_parquet(tmp_path / "curated/team_aliases/team_aliases.parquet")
    assert "auburn" in aliases.filter(pl.col("team_id") == "2")["alias_normalized"].to_list()


def test_build_quarantines_rejects_with_reasons(tmp_path: Path) -> None:
    seed_raw(tmp_path)
    board = one_game_board()
    board["events"][0]["competitions"][0]["competitors"][0]["score"] = "99"
    write_gz(raw_path(tmp_path, "NCAA", DAY, "scoreboard"), board)
    build_curated(tmp_path, tmp_path / "reports", today_et=date(2026, 11, 5), log=lambda _: None)
    assert not (tmp_path / "curated/games/league=NCAA/season=2025").exists()
    rejects = list((tmp_path / "curated/_rejects/games").glob("*.jsonl"))
    assert len(rejects) == 1
    row = json.loads(rejects[0].read_text("utf-8").splitlines()[0])
    assert row["reason"] == "periods_do_not_sum_to_score"
    assert row["game_id"] == GAME


def test_build_is_repeatable_and_does_not_duplicate_rejects(tmp_path: Path) -> None:
    seed_raw(tmp_path)
    board = one_game_board()
    board["events"][0]["competitions"][0]["competitors"][0]["score"] = "99"
    write_gz(raw_path(tmp_path, "NCAA", DAY, "scoreboard"), board)
    for _ in range(2):
        build_curated(tmp_path, tmp_path / "reports", date(2026, 11, 5), log=lambda _: None)
    rejects = next((tmp_path / "curated/_rejects/games").glob("*.jsonl"))
    assert len(rejects.read_text("utf-8").splitlines()) == 1


def test_coverage_report_flags_seasons_under_the_target(tmp_path: Path) -> None:
    seed_raw(tmp_path)
    summary = load("ncaa-20250118-summary.json")
    summary["boxscore"] = {"teams": [], "players": []}
    write_gz(raw_path(tmp_path, "NCAA", DAY, GAME), summary)
    reports = tmp_path / "reports"
    results = build_curated(tmp_path, reports, date(2026, 11, 5), log=lambda _: None)
    row = results["NCAA"]["2025"]
    assert row["final_games"] == 1
    assert row["pct_half"] == 100.0
    assert row["pct_box"] == 0.0
    assert row["meets_target"] is False
    text = (reports / "phase2-coverage.md").read_text("utf-8")
    assert "BELOW TARGET" in text
    assert (reports / "phase2-coverage.json").exists()


def test_team_rejected_from_nba_games_stays_out_of_the_registry(tmp_path: Path) -> None:
    seed_raw(tmp_path)
    day = date(2025, 1, 15)
    board = load("nba-20250115-scoreboard.json")
    event = next(e for e in board["events"] if str(e["id"]) == "401705127")
    stranger = event["competitions"][0]["competitors"][0]
    stranger["id"] = "99999"
    stranger["team"]["id"] = "99999"
    stranger["team"]["displayName"] = "Team Exhibition"
    board["events"] = [event]
    write_gz(raw_path(tmp_path, "NBA", day, "scoreboard"), board)
    build_curated(tmp_path, tmp_path / "reports", date(2026, 11, 5), log=lambda _: None)
    teams = pl.read_parquet(tmp_path / "curated/teams/teams.parquet")
    assert "99999" not in teams["team_id"].to_list()
    assert len(teams.filter(pl.col("league") == "NBA")) == 30


def test_d1_flag_uses_the_newest_ncaa_list_and_marks_others_false(tmp_path: Path) -> None:
    seed_raw(tmp_path)
    board = one_game_board()
    away = board["events"][0]["competitions"][0]["competitors"][1]
    away["id"] = "126"
    away["team"]["id"] = "126"
    away["team"]["displayName"] = "Kalamazoo Hornets"
    write_gz(raw_path(tmp_path, "NCAA", DAY, "scoreboard"), board)
    write_gz(raw_path(tmp_path, "NBA", date(2026, 11, 1), "scoreboard"), {"events": []})
    build_curated(tmp_path, tmp_path / "reports", date(2026, 11, 5), log=lambda _: None)
    teams = pl.read_parquet(tmp_path / "curated/teams/teams.parquet")
    kalamazoo = teams.filter(pl.col("team_id") == "126")
    assert kalamazoo["is_d1"].to_list() == [False]
    assert teams.filter(pl.col("team_id") == "61")["is_d1"].to_list() == [True]
    seasons = pl.read_parquet(tmp_path / "curated/team_seasons/team_seasons.parquet")
    row = seasons.filter((pl.col("team_id") == "126") & (pl.col("season") == 2025))
    assert row["is_d1"].to_list() == [False]


def test_team_ids_are_only_unique_within_a_league(tmp_path: Path) -> None:
    seed_raw(tmp_path)
    build_curated(tmp_path, tmp_path / "reports", date(2026, 11, 5), log=lambda _: None)
    teams = pl.read_parquet(tmp_path / "curated/teams/teams.parquet")
    same_id = teams.filter(pl.col("team_id") == "2").sort("league")
    assert same_id["league"].to_list() == ["NBA", "NCAA"]
    assert same_id["full_name"].to_list() == ["Boston Celtics", "Auburn Tigers"]
    assert teams.select(["league", "team_id"]).is_unique().all()
    aliases = pl.read_parquet(tmp_path / "curated/team_aliases/team_aliases.parquet")
    auburn = aliases.filter((pl.col("league") == "NCAA") & (pl.col("alias_normalized") == "auburn"))
    assert auburn["team_id"].to_list() == ["2"]
    assert aliases.filter(
        (pl.col("league") == "NBA") & (pl.col("alias_normalized") == "auburn")
    ).is_empty()
