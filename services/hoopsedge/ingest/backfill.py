"""Historical backfill and curated tables (docs/03-data-ingestion.md sections 2.2 to 2.5).

    docker compose up -d --build backfill                                   # fetch in background
    docker compose run --rm dev python -m hoopsedge.ingest.backfill status  # progress
    docker compose run --rm dev python -m hoopsedge.ingest.backfill build   # raw to curated
    docker compose run --rm dev python -m hoopsedge.ingest.backfill resume  # after a block

`fetch` only stores raw responses, and `build` rebuilds every curated table from those files,
so a parsing bug never needs a re-download. It runs in two stages: all games first, then
bookmaker odds, so stopping after the first stage still leaves complete game data. Stopping is
safe: finished dates are checkpointed and finished files are never fetched twice. A 401, 403,
451 or non-JSON response halts the whole run until the owner has reviewed it and run `resume`.
"""

import argparse
import gzip
import json
import os
import sys
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from hoopsedge.common.http import HttpFetchError, NotJsonError, PoliteClient
from hoopsedge.common.schemas import Game, PlayerGame, Reject, TeamGame
from hoopsedge.ingest.curated import write_parquet, write_table
from hoopsedge.ingest.espn_coverage import EASTERN, check_scoreboard_event
from hoopsedge.ingest.espn_parser import PLAYER_LEAGUES, parse_game
from hoopsedge.teams.registry import (
    TeamSighting,
    build_aliases,
    d1_ids_from_core,
    load_hard_cases,
    sighting_from_team,
    teams_from_list,
)

SITE = "https://site.api.espn.com/apis/site/v2/sports/basketball"
CORE = "https://sports.core.api.espn.com/v2/sports/basketball/leagues"
USER_AGENT = "HoopsEdge-backfill/0.1 (private research client)"
LEAGUES: dict[str, tuple[str, dict[str, str]]] = {
    "NBA": ("nba", {}),
    "NCAA": ("mens-college-basketball", {"groups": "50", "limit": "400"}),
}
# Owner decisions: NBA from 2018-19 (D39), NCAA from 2025-26 (D57). Lowering a value and
# running `fetch` again adds seasons without repeating anything.
FIRST_SEASON = {"NBA": 2019, "NCAA": 2026}
# (start month, day, end month, day), with the end in the season's closing year.
SEASON_WINDOW = {"NBA": (10, 1, 6, 30), "NCAA": (11, 1, 4, 15)}
# Seasons that ran outside the normal calendar.
WINDOW_OVERRIDES: dict[tuple[str, int], tuple[date, date]] = {
    ("NBA", 2020): (date(2019, 10, 1), date(2020, 10, 15)),
    ("NBA", 2021): (date(2020, 12, 1), date(2021, 7, 31)),
}
SETTLED_AFTER_DAYS = 2
GAMES = "games"
ODDS = "odds"
STAGES = (GAMES, ODDS)
PRESEASON = 1
TARGET_PERCENT = 99.0
BLOCK_STATUS = frozenset({401, 403, 451})
DEFAULT_DATA_DIR = "/workspace/data"
DEFAULT_REPORT_DIR = "/workspace/docs/reports"

Log = Callable[[str], None]


class BlockedError(RuntimeError):
    """ESPN refused us. The run stays stopped until the owner resumes it."""


@dataclass(frozen=True)
class Backfill:
    data_dir: Path
    client: PoliteClient
    today_et: date
    log: Log = print


def today_eastern() -> date:
    return datetime.now(UTC).astimezone(EASTERN).date()


def current_season(today: date) -> int:
    """The season a date belongs to, by its closing year. October onward is the next one."""
    return today.year + 1 if today.month >= 10 else today.year


def seasons_for(league: str, today: date) -> list[int]:
    """Newest first, so an early stop still leaves the most useful data."""
    return list(range(current_season(today), FIRST_SEASON[league] - 1, -1))


def season_dates(league: str, season: int, today: date) -> list[date]:
    start_month, start_day, end_month, end_day = SEASON_WINDOW[league]
    start, end = WINDOW_OVERRIDES.get(
        (league, season),
        (date(season - 1, start_month, start_day), date(season, end_month, end_day)),
    )
    # Yesterday's late games may still be playing, and a date once done is never revisited.
    # Live ingest covers yesterday onward.
    last = min(end, today - timedelta(days=SETTLED_AFTER_DAYS))
    return [start + timedelta(days=i) for i in range((last - start).days + 1)]


def raw_path(data_dir: Path, league: str, day: date, name: str) -> Path:
    return data_dir / f"raw/espn/{league}/{day:%Y/%m/%d}/{name}.json.gz"


def write_gz(path: Path, document: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(gzip.compress(json.dumps(document).encode("utf-8")))
    os.replace(temporary, path)


def read_gz(path: Path) -> Any:
    return json.loads(gzip.decompress(path.read_bytes()))


def halt_path(data_dir: Path) -> Path:
    return data_dir / "backfill" / "HALTED"


def assert_not_halted(data_dir: Path) -> None:
    path = halt_path(data_dir)
    if path.exists():
        raise BlockedError(f"Backfill is halted: {path.read_text('utf-8')}")


def halt_on_block(data_dir: Path, error: HttpFetchError) -> None:
    """If this error is a block, record a halt that lasts until the owner clears it, and raise."""
    if isinstance(error, NotJsonError) or error.status in BLOCK_STATUS:
        path = halt_path(data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{datetime.now(UTC).isoformat()} {error}", "utf-8")
        raise BlockedError(str(error)) from error


def fetch_json(backfill: Backfill, url: str, params: dict[str, str] | None = None) -> Any:
    """GET, and turn any block into a persistent halt."""
    try:
        return backfill.client.get_json(url, params)
    except HttpFetchError as error:
        halt_on_block(backfill.data_dir, error)
        raise


def fetch_registry(backfill: Backfill) -> None:
    """Store team lists: current teams for both leagues and D1 membership per NCAA season."""
    assert_not_halted(backfill.data_dir)
    folder = backfill.data_dir / "raw/espn/registry"
    calls: list[tuple[Path, str, dict[str, str]]] = [
        (folder / f"{league}-teams.json.gz", f"{SITE}/{path}/teams", {"limit": "1000"})
        for league, (path, _) in LEAGUES.items()
    ]
    path = LEAGUES["NCAA"][0]
    calls += [
        (
            folder / f"NCAA-d1-{season}.json.gz",
            f"{CORE}/{path}/seasons/{season}/types/2/groups/50/teams",
            {"limit": "500"},
        )
        for season in seasons_for("NCAA", backfill.today_et)
    ]
    for file, url, params in calls:
        if not file.exists():
            write_gz(file, fetch_json(backfill, url, params))


def _wants_summary(event: dict[str, Any], league: str) -> bool:
    check = check_scoreboard_event(event, league)
    return check.is_final and not check.is_exhibition and check.season_type != PRESEASON


def _fetch_games_date(backfill: Backfill, league: str, season: int, day: date) -> dict[str, str]:
    """Fetch one date's scoreboard and final-game summaries. Returns failures by game ID."""
    path, params = LEAGUES[league]
    board_file = raw_path(backfill.data_dir, league, day, "scoreboard")
    if board_file.exists():
        board = read_gz(board_file)
    else:
        try:
            board = fetch_json(
                backfill,
                f"{SITE}/{path}/scoreboard",
                {"dates": day.strftime("%Y%m%d"), **params},
            )
        except HttpFetchError as error:
            return {"scoreboard": str(error)}
        write_gz(board_file, board)  # Docs/03 section 2.1: raw is written before parsing.

    failures: dict[str, str] = {}
    for event in board.get("events", []):
        game_id = str(event["id"])
        summary_file = raw_path(backfill.data_dir, league, day, game_id)
        if not _wants_summary(event, league) or summary_file.exists():
            continue
        try:
            write_gz(
                summary_file, fetch_json(backfill, f"{SITE}/{path}/summary", {"event": game_id})
            )
        except HttpFetchError as error:
            failures[game_id] = str(error)
    return failures


def _d1_ids(data_dir: Path, league: str, season: int) -> set[str] | None:
    file = data_dir / f"raw/espn/registry/NCAA-d1-{season}.json.gz"
    return d1_ids_from_core(read_gz(file)) if league == "NCAA" and file.exists() else None


def _wants_odds(event: dict[str, Any], league: str, d1_ids: set[str] | None) -> bool:
    """Final games, leaving out NCAA games against non-D1 teams, which ESPN rarely prices."""
    if not _wants_summary(event, league):
        return False
    competitors = event["competitions"][0].get("competitors", [])
    return d1_ids is None or all(str(c["id"]) in d1_ids for c in competitors)


def _fetch_odds_date(backfill: Backfill, league: str, season: int, day: date) -> dict[str, str]:
    """Fetch core-API odds (D27) for one date, after its scoreboard has been stored."""
    path, _ = LEAGUES[league]
    board_file = raw_path(backfill.data_dir, league, day, "scoreboard")
    if not board_file.exists():
        return {"scoreboard": "not fetched yet"}
    d1_ids = _d1_ids(backfill.data_dir, league, season)
    failures: dict[str, str] = {}
    for event in read_gz(board_file).get("events", []):
        game_id = str(event["id"])
        odds_file = raw_path(backfill.data_dir, league, day, f"{game_id}.odds")
        if not _wants_odds(event, league, d1_ids) or odds_file.exists():
            continue
        url = f"{CORE}/{path}/events/{game_id}/competitions/{game_id}/odds"
        try:
            write_gz(odds_file, fetch_json(backfill, url, {"limit": "50"}))
        except HttpFetchError as error:
            if error.status == 404:  # ESPN holds no odds for this game.
                write_gz(odds_file, {"items": [], "not_found": True})
            else:
                failures[game_id] = str(error)
    return failures


def _checkpoint_path(data_dir: Path, league: str, season: int, stage: str) -> Path:
    suffix = "" if stage == GAMES else f"-{stage}"
    return data_dir / "backfill" / "checkpoints" / f"{league}-{season}{suffix}.json"


def _read_checkpoint(
    data_dir: Path, league: str, season: int, stage: str = "games"
) -> dict[str, Any]:
    path = _checkpoint_path(data_dir, league, season, stage)
    if path.exists():
        checkpoint: dict[str, Any] = json.loads(path.read_text("utf-8"))
        return checkpoint
    return {"completed_dates": [], "failures": {}}


def _write_checkpoint(
    data_dir: Path, league: str, season: int, stage: str, checkpoint: dict[str, Any]
) -> None:
    path = _checkpoint_path(data_dir, league, season, stage)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(checkpoint, sort_keys=True), "utf-8")
    os.replace(temporary, path)


def fetch_season(
    backfill: Backfill, league: str, season: int, days: Sequence[date], stage: str = "games"
) -> bool:
    """Run one stage for the given dates. True means every one of them is now complete."""
    assert_not_halted(backfill.data_dir)
    fetch_date = _fetch_games_date if stage == GAMES else _fetch_odds_date
    checkpoint = _read_checkpoint(backfill.data_dir, league, season, stage)
    completed = set(checkpoint["completed_dates"])
    failures: dict[str, dict[str, str]] = checkpoint["failures"]
    for day in days:
        key = day.isoformat()
        if key in completed:
            continue
        found = fetch_date(backfill, league, season, day)
        if found:
            failures[key] = found
        else:
            failures.pop(key, None)
            completed.add(key)
        checkpoint = {"completed_dates": sorted(completed), "failures": failures}
        _write_checkpoint(backfill.data_dir, league, season, stage, checkpoint)
    return all(day.isoformat() in completed for day in days)


def fetch_all(backfill: Backfill, only: str | None = None) -> bool:
    fetch_registry(backfill)
    complete = True
    leagues = [only] if only else list(LEAGUES)
    for stage in STAGES:
        for league in leagues:
            for season in seasons_for(league, backfill.today_et):
                days = season_dates(league, season, backfill.today_et)
                started = time.monotonic()
                before = backfill.client.stats.requests
                done = fetch_season(backfill, league, season, days, stage)
                complete = complete and done
                backfill.log(
                    f"{stage} {league} {season}: {'complete' if done else 'INCOMPLETE'}, "
                    f"{backfill.client.stats.requests - before} requests, "
                    f"{time.monotonic() - started:.0f} s"
                )
    return complete


@dataclass
class _SeasonBuild:
    games: list[Game]
    team_games: list[TeamGame]
    player_games: list[PlayerGame]
    rejects: list[Reject]
    sightings: dict[str, tuple[TeamSighting, str | None]]
    other_statuses: Counter[str]
    final_seen: int
    summaries_absent: int


def _build_season(
    data_dir: Path, league: str, season: int, today: date, nba_ids: set[str]
) -> _SeasonBuild:
    build = _SeasonBuild([], [], [], [], {}, Counter(), 0, 0)
    known = nba_ids if league == "NBA" else None
    for day in season_dates(league, season, today):
        board_file = raw_path(data_dir, league, day, "scoreboard")
        if not board_file.exists():
            continue
        for event in read_gz(board_file).get("events", []):
            check = check_scoreboard_event(event, league)
            if check.is_exhibition or check.season_type == PRESEASON:
                continue
            for competitor in event["competitions"][0].get("competitors", []):
                team = competitor.get("team") or {}
                if team.get("id") and (known is None or str(team["id"]) in known):
                    build.sightings[str(team["id"])] = (
                        sighting_from_team(league, team),
                        str(team["conferenceId"]) if team.get("conferenceId") else None,
                    )
            if not check.is_final:
                status = event["competitions"][0].get("status", {}).get("type", {}).get("name")
                build.other_statuses[str(status)] += 1
                continue
            build.final_seen += 1
            game_id = str(event["id"])
            summary_file = raw_path(data_dir, league, day, game_id)
            summary = read_gz(summary_file) if summary_file.exists() else None
            build.summaries_absent += summary is None
            parsed = parse_game(league, event, summary, known)
            if parsed.game is not None:
                build.games.append(parsed.game)
            build.team_games += parsed.team_games
            build.player_games += parsed.player_games
            build.rejects += parsed.rejects
    return build


def _percent(part: int, whole: int) -> float | None:
    return round(100 * part / whole, 2) if whole else None


def _coverage_row(build: _SeasonBuild) -> dict[str, Any]:
    final = build.final_seen
    with_box = sum(not game.box_missing for game in build.games)
    pct_half = _percent(len(build.games), final)
    pct_box = _percent(with_box, final)
    return {
        "final_games": final,
        "games_kept": len(build.games),
        "pct_half": pct_half,
        "pct_box": pct_box,
        "game_rejects": sum(reject.table == "games" for reject in build.rejects),
        "box_rejects": sum(reject.table == "team_games" for reject in build.rejects),
        "reject_reasons": dict(Counter(reject.reason for reject in build.rejects)),
        "summaries_absent": build.summaries_absent,
        "other_statuses": dict(build.other_statuses),
        "meets_target": bool(final)
        and (pct_half or 0) >= TARGET_PERCENT
        and (pct_box or 0) >= TARGET_PERCENT,
    }


def _write_registry(
    data_dir: Path,
    sightings: dict[str, dict[str, tuple[TeamSighting, str | None]]],
    season_rows: list[dict[str, Any]],
    log: Log,
) -> None:
    registry = data_dir / "raw/espn/registry"
    hard_cases = load_hard_cases()
    team_rows: list[dict[str, Any]] = []
    alias_rows: list[dict[str, Any]] = []
    ambiguous_rows: dict[str, dict[str, list[str]]] = {}
    d1_lists = sorted(
        registry.glob("NCAA-d1-*.json.gz"), key=lambda file: int(file.name.split(".")[0][-4:])
    )
    latest_d1 = d1_ids_from_core(read_gz(d1_lists[-1])) if d1_lists else set()
    for league in LEAGUES:
        listed = teams_from_list(league, read_gz(registry / f"{league}-teams.json.gz"))
        merged = {team_id: seen for team_id, (seen, _) in sightings[league].items()}
        merged.update({team.team_id: team for team in listed})
        conferences = {team_id: conf for team_id, (_, conf) in sightings[league].items()}
        ever_d1 = {r["team_id"] for r in season_rows if r["league"] == league and r["is_d1"]}
        for team in merged.values():
            team_rows.append(
                {
                    **asdict(team),
                    "conference_id": conferences.get(team.team_id),
                    "is_d1": league == "NBA" or team.team_id in latest_d1,
                }
            )
        aliased = [
            t for t in merged.values() if league == "NBA" or t.team_id in ever_d1 or t in listed
        ]
        aliases, ambiguous = build_aliases(league, aliased, hard_cases[league])
        alias_rows += [
            {**asdict(alias), "confirmed_by": None, "confirmed_at": None} for alias in aliases
        ]
        ambiguous_rows[league] = ambiguous
        log(
            f"{league} registry: {len(merged)} teams, {len(aliases)} aliases, "
            f"{len(ambiguous)} ambiguous"
        )
    alias_schema = {
        "league": pl.String,
        "alias_normalized": pl.String,
        "team_id": pl.String,
        "source": pl.String,
        "confirmed_by": pl.String,
        "confirmed_at": pl.String,
    }
    write_parquet(
        pl.DataFrame(team_rows, infer_schema_length=None), data_dir / "curated/teams/teams.parquet"
    )
    write_parquet(
        pl.DataFrame(alias_rows, schema=alias_schema),
        data_dir / "curated/team_aliases/team_aliases.parquet",
    )
    write_parquet(pl.DataFrame(season_rows), data_dir / "curated/team_seasons/team_seasons.parquet")
    write_gz(data_dir / "curated/team_aliases/ambiguous.json.gz", ambiguous_rows)


def build_curated(
    data_dir: Path, report_dir: Path, today_et: date, log: Log = print
) -> dict[str, Any]:
    """Rebuild all curated tables and the coverage report from raw files."""
    registry = data_dir / "raw/espn/registry"
    nba_ids = {t.team_id for t in teams_from_list("NBA", read_gz(registry / "NBA-teams.json.gz"))}
    results: dict[str, Any] = {}
    all_rejects: list[Reject] = []
    sightings: dict[str, dict[str, tuple[TeamSighting, str | None]]] = {
        league: {} for league in LEAGUES
    }
    season_rows: list[dict[str, Any]] = []
    for league in LEAGUES:
        results[league] = {}
        for season in sorted(seasons_for(league, today_et)):
            build = _build_season(data_dir, league, season, today_et, nba_ids)
            write_table(data_dir, "games", league, season, build.games)
            write_table(data_dir, "team_games", league, season, build.team_games)
            if league in PLAYER_LEAGUES:
                write_table(data_dir, "player_games", league, season, build.player_games)
            all_rejects += build.rejects
            sightings[league].update(build.sightings)
            d1_file = registry / f"NCAA-d1-{season}.json.gz"
            d1_ids = (
                d1_ids_from_core(read_gz(d1_file))
                if league == "NCAA" and d1_file.exists()
                else None
            )
            for team_id in build.sightings:
                season_rows.append(
                    {
                        "league": league,
                        "season": season,
                        "team_id": team_id,
                        "is_d1": True if league == "NBA" else bool(d1_ids and team_id in d1_ids),
                    }
                )
            checkpoint = _read_checkpoint(data_dir, league, season)
            row = _coverage_row(build)
            row["dates_complete"] = len(checkpoint["completed_dates"])
            row["dates_total"] = len(season_dates(league, season, today_et))
            results[league][str(season)] = row
            if build.final_seen:
                log(
                    f"{league} {season}: {build.final_seen} finals, half {row['pct_half']}%, "
                    f"box {row['pct_box']}%"
                )

    _write_registry(data_dir, sightings, season_rows, log)
    _write_rejects(data_dir, all_rejects, today_et)
    results["generated_at"] = datetime.now(UTC).isoformat()
    _write_report(report_dir, results)
    return results


def _write_rejects(data_dir: Path, rejects: Sequence[Reject], today: date) -> None:
    """Each build reflects all raw data, so it replaces earlier reject files."""
    folder = data_dir / "curated/_rejects"
    for table in ("games", "team_games"):
        directory = folder / table
        directory.mkdir(parents=True, exist_ok=True)
        for old in directory.glob("*.jsonl"):
            old.unlink()
        rows = [reject for reject in rejects if reject.table == table]
        if rows:
            lines = "".join(row.model_dump_json() + "\n" for row in rows)
            (directory / f"{today.isoformat()}.jsonl").write_text(lines, "utf-8")


def _write_report(report_dir: Path, results: dict[str, Any]) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "phase2-coverage.json").write_text(
        json.dumps(results, indent=1, sort_keys=True) + "\n", "utf-8"
    )
    lines = [
        "# Phase 2: Backfill Coverage",
        "",
        f"Generated by `hoopsedge.ingest.backfill build` on {results['generated_at'][:10]}. "
        f"The target is at least {TARGET_PERCENT:g}% of final games with half scores and "
        "box totals.",
    ]
    for league in LEAGUES:
        lines += [
            "",
            f"## {league}",
            "",
            "| Season | Dates done | Final games | Half % | Box % | Rejects (game / box) "
            "| No summary | Other statuses | Target |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for season, row in sorted(results[league].items()):
            statuses = ", ".join(f"{k} {v}" for k, v in sorted(row["other_statuses"].items()))
            verdict = "met" if row["meets_target"] else "BELOW TARGET"
            if not row["final_games"]:
                verdict = "no data yet"
            lines.append(
                f"| {season} | {row['dates_complete']} of {row['dates_total']} "
                f"| {row['final_games']} | {row['pct_half']} | {row['pct_box']} "
                f"| {row['game_rejects']} / {row['box_rejects']} | {row['summaries_absent']} "
                f"| {statuses or 'none'} | {verdict} |"
            )
    (report_dir / "phase2-coverage.md").write_text("\n".join(lines) + "\n", "utf-8")


def status_text(data_dir: Path, today: date) -> str:
    lines = []
    halt = halt_path(data_dir)
    if halt.exists():
        lines.append(f"HALTED: {halt.read_text('utf-8')}")
    for stage in STAGES:
        total_done = total_dates = 0
        for league in LEAGUES:
            for season in seasons_for(league, today):
                checkpoint = _read_checkpoint(data_dir, league, season, stage)
                done = len(checkpoint["completed_dates"])
                dates = len(season_dates(league, season, today))
                total_done += done
                total_dates += dates
                failed = sum(len(games) for games in checkpoint["failures"].values())
                if done or failed:
                    problem = f", {failed} failed" if failed else ""
                    lines.append(f"{stage}: {league} {season}: {done} of {dates} dates{problem}")
        lines.append(f"{stage} overall: {total_done} of {total_dates} dates")
    raw = data_dir / "raw/espn"
    files = [p for league in LEAGUES for p in (raw / league).rglob("*.json.gz")]
    size = sum(p.stat().st_size for p in files)
    lines.append(f"Raw: {len(files)} files, {size / 1_000_000_000:.2f} GB")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    fetch = commands.add_parser("fetch")
    fetch.add_argument("--league", choices=sorted(LEAGUES))
    fetch.add_argument("--season", type=int, help="With --league and --dates, fetch only a sample.")
    fetch.add_argument("--dates", nargs="*", help="YYYY-MM-DD dates for a sample fetch.")
    commands.add_parser("build")
    commands.add_parser("status")
    commands.add_parser("resume")
    args = parser.parse_args()

    data_dir = Path(os.environ.get("HOOPSEDGE_DATA_DIR", DEFAULT_DATA_DIR))
    today = today_eastern()
    if args.command == "status":
        print(status_text(data_dir, today))
        return 0
    if args.command == "resume":
        halt_path(data_dir).unlink(missing_ok=True)
        print("Halt cleared.")
        return 0
    if args.command == "build":
        report_dir = Path(os.environ.get("HOOPSEDGE_REPORT_DIR", DEFAULT_REPORT_DIR))
        build_curated(data_dir, report_dir, today)
        return 0

    client = PoliteClient(USER_AGENT, rate_per_second=2.0)
    backfill = Backfill(data_dir, client, today)
    try:
        if args.dates:
            fetch_registry(backfill)
            days = [date.fromisoformat(value) for value in args.dates]
            done = all(
                fetch_season(backfill, args.league, args.season, days, stage) for stage in STAGES
            )
        else:
            done = fetch_all(backfill, args.league)
    except BlockedError as error:
        print(f"HALTED: {error}", file=sys.stderr)
        return 2
    finally:
        client.close()
    stats = client.stats
    print(f"Finished. Complete: {done}. Requests: {stats.requests}, {dict(stats.by_status)}")
    return 0 if done else 1


if __name__ == "__main__":
    sys.exit(main())
