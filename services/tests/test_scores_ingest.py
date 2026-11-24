"""v2 scoreboard ingest (A1): incremental refresh, levels, deduped feeds, rejects, halts."""

import copy
import json
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import polars as pl
import pytest
import respx

from hoopsedge.common.http import PoliteClient
from hoopsedge.ingest.espn_files import BlockedError, Fetcher, halt_path, write_gz
from hoopsedge.ingest.scores import (
    CORE_ROOT,
    OTHER,
    SITE_ROOT,
    SOURCES,
    IngestError,
    board_path,
    build_season,
    dates_to_fetch,
    level_list_path,
    read_checkpoint,
    read_results,
    read_teams,
    refresh,
    season_for,
    season_window,
    write_season,
)
from hoopsedge.pipeline.schedule import due_jobs

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "espn"
NCAAF = SOURCES["NCAAF"]
# A short window keeps request counts readable: 19 November to today plus the lookahead.
SHORT = replace(NCAAF, season_starts=(11, 19))
TODAY = date(2026, 11, 24)
GAME_DAY = date(2026, 11, 21)
# Written out in full, so a wrong URL in the code can't pass by mocking the same mistake.
BOARD_URL = f"{SITE_ROOT}/football/college-football/scoreboard"
CORE_SEASON = f"{CORE_ROOT}/football/leagues/college-football/seasons/2026/types/2"
SHARED = "401856746"  # Chattanooga at Alabama, FCS at FBS, in both feeds.
FBS_GAME = "401856753"  # Georgia at South Carolina.
FCS_GAME = "401866694"  # UAlbany at North Carolina A&T.


def load(name: str) -> dict[str, Any]:
    document: dict[str, Any] = json.loads((FIXTURES / name).read_text("utf-8"))
    return document


def boards() -> dict[str, dict[str, Any]]:
    return {group: load(f"ncaaf-20261121-scoreboard-{group}.json") for group in ("80", "81")}


@pytest.fixture
def backfill(tmp_path: Path) -> Fetcher:
    client = PoliteClient("t", rate_per_second=1000.0, max_retries=0, sleep=lambda _: None)
    return Fetcher(tmp_path, client, today_et=TODAY, log=lambda _: None)


def mock_level_lists() -> None:
    for group in ("80", "81"):
        respx.get(f"{CORE_SEASON}/groups/{group}/teams").mock(
            return_value=httpx.Response(200, json=load(f"ncaaf-2026-group-{group}-teams.json"))
        )


def mock_espn(game_boards: dict[str, dict[str, Any]] | None = None) -> respx.Route:
    """Game day answers with the stored boards; every other date is empty. Routes added
    before this one win, since respx tries them in order."""
    stored = game_boards or boards()

    def answer(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        if params["dates"] == f"{GAME_DAY:%Y%m%d}":
            return httpx.Response(200, json=stored[params["groups"]])
        return httpx.Response(200, json={"events": []})

    mock_level_lists()
    return respx.get(BOARD_URL).mock(side_effect=answer)


def store(data_dir: Path, game_boards: dict[str, dict[str, Any]] | None = None) -> None:
    """Put the game day and the level lists on disk without any HTTP."""
    for group, board in (game_boards or boards()).items():
        write_gz(board_path(data_dir, "NCAAF", GAME_DAY, group), board)
        write_gz(
            level_list_path(data_dir, "NCAAF", 2026, group),
            load(f"ncaaf-2026-group-{group}-teams.json"),
        )


def fetched_dates(route: respx.Route, since: int = 0) -> list[str]:
    return sorted({call.request.url.params["dates"] for call in list(route.calls)[since:]})


def test_football_seasons_are_named_by_their_first_year() -> None:
    assert season_for(date(2026, 8, 29), NCAAF) == 2026
    assert season_for(date(2026, 11, 28), NCAAF) == 2026
    assert season_for(date(2027, 1, 12), NCAAF) == 2026  # The bowls.
    assert season_for(date(2027, 8, 20), NCAAF) == 2027


def test_basketball_seasons_are_named_by_their_last_year() -> None:
    for sport in ("NCAAB", "NBA"):
        source = SOURCES[sport]
        assert season_for(date(2026, 11, 24), source) == 2027
        assert season_for(date(2027, 3, 20), source) == 2027
    assert season_window(SOURCES["NCAAB"], 2027) == (date(2026, 11, 1), date(2027, 4, 15))
    assert season_window(SOURCES["NBA"], 2027) == (date(2026, 10, 1), date(2027, 6, 30))


def test_dates_run_from_season_start_to_the_lookahead() -> None:
    days = dates_to_fetch(SHORT, 2026, TODAY, settled=set())
    assert days[0] == date(2026, 11, 19)
    assert days[-1] == date(2026, 12, 1)
    assert dates_to_fetch(SHORT, 2026, TODAY, settled={"2026-11-19"})[0] == date(2026, 11, 20)


@respx.mock
def test_first_refresh_backfills_and_a_rerun_fetches_only_recent_days(backfill: Fetcher) -> None:
    route = mock_espn()
    first = refresh(backfill, SHORT, 2026)
    assert not first["failures"]
    assert len(fetched_dates(route)) == 13  # 19 November to 1 December.
    assert route.call_count == 26  # Both groups for every date.
    # 21 November and older is settled; the last 3 days and the lookahead are not.
    assert read_checkpoint(backfill.data_dir, "NCAAF", 2026)["settled_dates"] == [
        "2026-11-19",
        "2026-11-20",
        "2026-11-21",
    ]

    boards_before, all_before = route.call_count, len(respx.calls)
    refresh(backfill, SHORT, 2026)
    assert fetched_dates(route, boards_before)[0] == "20261122"
    assert len(fetched_dates(route, boards_before)) == 10
    assert len(respx.calls) - all_before == 20  # No level lists either: they're kept per season.


@respx.mock
def test_an_old_date_with_an_unfinished_game_is_fetched_again(backfill: Fetcher) -> None:
    game_boards = boards()
    game_boards["81"]["events"][1]["competitions"][0]["status"]["type"]["state"] = "pre"
    route = mock_espn(game_boards)
    refresh(backfill, SHORT, 2026)
    assert "2026-11-21" not in read_checkpoint(backfill.data_dir, "NCAAF", 2026)["settled_dates"]
    before = route.call_count
    refresh(backfill, SHORT, 2026)
    assert "20261121" in fetched_dates(route, before)


@respx.mock
def test_a_failed_date_is_reported_and_retried(backfill: Fetcher) -> None:
    respx.get(BOARD_URL, params={"dates": "20261120", "groups": "81"}).mock(
        return_value=httpx.Response(500)
    )
    mock_espn()
    outcome = refresh(backfill, SHORT, 2026)
    assert list(outcome["failures"]) == ["2026-11-20"]
    checkpoint = read_checkpoint(backfill.data_dir, "NCAAF", 2026)
    assert "2026-11-20" not in checkpoint["settled_dates"]
    assert "2026-11-20" in checkpoint["failures"]


@respx.mock
def test_a_block_halts_every_espn_job(backfill: Fetcher) -> None:
    mock_level_lists()
    respx.get(BOARD_URL).mock(return_value=httpx.Response(403))
    with pytest.raises(BlockedError):
        refresh(backfill, SHORT, 2026)
    assert halt_path(backfill.data_dir).exists()
    with pytest.raises(BlockedError):
        refresh(backfill, SHORT, 2026)


def test_build_keeps_one_row_per_game_with_final_scores(tmp_path: Path) -> None:
    store(tmp_path)
    build = write_season(tmp_path, NCAAF, 2026)
    assert build.rejects == []
    results = read_results(tmp_path, "NCAAF", 2026)
    assert sorted(results["game_id"].to_list()) == sorted([SHARED, FBS_GAME, FCS_GAME])
    game = results.filter(pl.col("game_id") == SHARED).row(0, named=True)
    assert (game["home_id"], game["away_id"]) == ("333", "236")
    assert (game["home_pts"], game["away_pts"]) == (47, 0)  # Read from the real response.
    assert game["completed"] is True
    assert game["kickoff_utc"].tzinfo is not None
    assert game["slate_date_et"] == GAME_DAY


def test_registry_holds_names_and_level_from_espn_lists(tmp_path: Path) -> None:
    store(tmp_path)
    write_season(tmp_path, NCAAF, 2026)
    teams = {row["team_id"]: row for row in read_teams(tmp_path, "NCAAF", 2026).to_dicts()}
    assert {team_id: row["level"] for team_id, row in teams.items()} == {
        "61": "FBS",
        "333": "FBS",
        "2579": "FBS",
        "236": "FCS",
        "399": "FCS",
        "2448": "FCS",
    }
    alabama = teams["333"]
    assert (alabama["display_name"], alabama["location"], alabama["mascot"]) == (
        "Alabama Crimson Tide",
        "Alabama",
        "Crimson Tide",
    )


def test_a_team_on_neither_list_is_other(tmp_path: Path) -> None:
    store(tmp_path)
    lists = load("ncaaf-2026-group-81-teams.json")
    lists["items"] = [item for item in lists["items"] if "/teams/399?" not in item["$ref"]]
    write_gz(level_list_path(tmp_path, "NCAAF", 2026, "81"), lists)
    teams = {row["team_id"]: row["level"] for row in build_season(tmp_path, NCAAF, 2026).teams}
    assert teams["399"] == OTHER


def test_a_team_on_two_lists_stops_the_build(tmp_path: Path) -> None:
    store(tmp_path)
    lists = load("ncaaf-2026-group-81-teams.json")
    lists["items"].append(load("ncaaf-2026-group-80-teams.json")["items"][0])
    write_gz(level_list_path(tmp_path, "NCAAF", 2026, "81"), lists)
    with pytest.raises(IngestError, match="both FBS and FCS"):
        build_season(tmp_path, NCAAF, 2026)


def test_a_missing_level_list_stops_the_build(tmp_path: Path) -> None:
    store(tmp_path)
    level_list_path(tmp_path, "NCAAF", 2026, "80").unlink()
    with pytest.raises(IngestError, match="No FBS team list"):
        build_season(tmp_path, NCAAF, 2026)


def test_feeds_that_disagree_on_a_game_reject_it(tmp_path: Path) -> None:
    game_boards = boards()
    shared = next(e for e in game_boards["81"]["events"] if e["id"] == SHARED)
    shared["competitions"][0]["competitors"][0]["score"] = "99"
    shared["competitions"][0]["competitors"][0]["linescores"] = [{"value": 99}]
    store(tmp_path, game_boards)
    build = build_season(tmp_path, NCAAF, 2026)
    assert {"game_id": SHARED, "reason": "feeds_disagree"} in build.rejects
    assert SHARED not in {row["game_id"] for row in build.results}


def test_quarters_that_do_not_sum_to_the_score_reject_the_game(tmp_path: Path) -> None:
    game_boards = boards()
    competitor = game_boards["81"]["events"][1]["competitions"][0]["competitors"][0]
    competitor["score"] = str(int(competitor["score"]) + 3)
    store(tmp_path, game_boards)
    build = build_season(tmp_path, NCAAF, 2026)
    assert build.rejects == [{"game_id": FCS_GAME, "reason": "periods_do_not_sum_to_score"}]


def test_a_scheduled_game_has_no_score(tmp_path: Path) -> None:
    """ESPN shows 0-0 before kickoff. That must never reach the L5 window as a result."""
    game_boards = boards()
    event = copy.deepcopy(game_boards["80"]["events"][0])
    competition = event["competitions"][0]
    competition["status"]["type"].update(
        {"name": "STATUS_SCHEDULED", "state": "pre", "completed": False}
    )
    for competitor in competition["competitors"]:
        competitor["score"] = "0"
        competitor.pop("linescores", None)
    game_boards["80"]["events"] = [event]
    game_boards["81"]["events"] = []
    store(tmp_path, game_boards)
    [row] = build_season(tmp_path, NCAAF, 2026).results
    assert row["completed"] is False
    assert (row["home_pts"], row["away_pts"]) == (None, None)


def test_scheduler_refreshes_every_sport_each_morning() -> None:
    eight_in_lagos = datetime(2026, 11, 24, 7, 0, tzinfo=UTC)
    jobs = {job.name: job for job in due_jobs(eight_in_lagos, {})}
    assert set(jobs) == {"scores-NCAAF", "scores-NCAAB", "scores-NBA"}
    command = ("hoopsedge.ingest.scores", "refresh", "--sport", "NCAAF")
    assert jobs["scores-NCAAF"].command[-4:] == command
    done = {name: "2026-11-24" for name in jobs}
    assert not due_jobs(eight_in_lagos, done)


# Basketball (B2)

NBA = SOURCES["NBA"]
NCAAB = SOURCES["NCAAB"]
NBA_DAY = date(2026, 11, 4)


def nba_board() -> dict[str, Any]:
    return load("nba-20261104-scoreboard.json")


def test_nba_has_one_feed_and_every_team_is_nba_level(tmp_path: Path) -> None:
    assert NBA.feeds == (None,)
    write_gz(board_path(tmp_path, "NBA", NBA_DAY, None), nba_board())
    build = build_season(tmp_path, NBA, 2027)
    assert len(build.results) == 3 and build.rejects == []
    assert {row["level"] for row in build.teams} == {"NBA"}


def test_preseason_and_all_star_games_are_left_out(tmp_path: Path) -> None:
    board = nba_board()
    board["events"][0]["season"]["type"] = 1
    board["events"][1]["competitions"][0]["type"] = {"abbreviation": "ALLSTAR"}
    write_gz(board_path(tmp_path, "NBA", NBA_DAY, None), board)
    build = build_season(tmp_path, NBA, 2027)
    assert [row["game_id"] for row in build.results] == [board["events"][2]["id"]]
    assert build.skipped == 2
    playing = {c["id"] for c in board["events"][2]["competitions"][0]["competitors"]}
    assert {row["team_id"] for row in build.teams} == playing


@respx.mock
def test_nba_scoreboard_is_fetched_without_groups(tmp_path: Path) -> None:
    client = PoliteClient("t", rate_per_second=1000.0, max_retries=0, sleep=lambda _: None)
    fetcher = Fetcher(tmp_path, client, today_et=date(2026, 10, 2), log=lambda _: None)
    route = respx.get(f"{SITE_ROOT}/basketball/nba/scoreboard").mock(
        return_value=httpx.Response(200, json={"events": []})
    )
    refresh(fetcher, NBA, 2027)
    assert route.call_count == 9  # 1 to 9 October: the season start to the lookahead.
    assert all("groups" not in call.request.url.params for call in route.calls)


def test_ncaab_level_comes_from_the_d1_list(tmp_path: Path) -> None:
    day = date(2025, 1, 18)
    write_gz(board_path(tmp_path, "NCAAB", day, "50"), load("ncaa-20250118-scoreboard.json"))
    d1 = load("d1-teams-2027.json")
    d1["items"] = [item for item in d1["items"] if "/teams/2?" not in item["$ref"]]
    write_gz(level_list_path(tmp_path, "NCAAB", 2025, "50"), d1)
    teams = {row["team_id"]: row["level"] for row in build_season(tmp_path, NCAAB, 2025).teams}
    assert teams["61"] == "D1"
    assert teams["2"] == OTHER  # Taken off the list above.
