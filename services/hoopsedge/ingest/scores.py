"""v2 scoreboard ingest: final scores, schedules and team levels for the L5 model (brief §3).

    docker compose run --rm dev python -m hoopsedge.ingest.scores refresh --sport NCAAF
    docker compose run --rm dev python -m hoopsedge.ingest.scores status --sport NCAAF

The L5 model needs only who played whom, when, and the final score, so this reads ESPN's
scoreboard and nothing else: no summaries, boxes or odds. The first `refresh` of a season is its
backfill. After that, a date is fetched again only while it is unsettled: younger than
`SETTLED_AFTER_DAYS`, or holding a game that hasn't ended. The next `LOOKAHEAD_DAYS` are always
fetched, so upcoming kickoffs are known for matching (brief §5.3 step 6).

Level comes from ESPN's per-season group lists (FBS is group 80, FCS 81). A team on neither list
is `OTHER`. A team on two lists, or a missing list, stops the build: a guessed level would hide
the cross-level games the brief flags (§4.3).

Raw responses go to `raw/espn/v2/{sport}/`, and `build` rewrites the curated tables from them:
`curated/results/sport={sport}/season={season}/part-0.parquet` and
`curated/registry/sport={sport}/season={season}/teams.parquet`. A 401, 403, 451 or non-JSON
response halts every ESPN job until the owner runs `python -m hoopsedge.ingest.backfill resume`.
"""

import argparse
import json
import os
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from hoopsedge.common.http import HttpFetchError, PoliteClient
from hoopsedge.ingest.backfill import (
    Backfill,
    BlockedError,
    assert_not_halted,
    fetch_json,
    halt_path,
    read_gz,
    today_eastern,
    write_gz,
)
from hoopsedge.ingest.curated import write_parquet
from hoopsedge.ingest.espn_coverage import et_date, parse_time, to_number

SITE_ROOT = "https://site.api.espn.com/apis/site/v2/sports"
CORE_ROOT = "https://sports.core.api.espn.com/v2/sports"
USER_AGENT = "HoopsEdge-scores/0.2 (private research client)"
SETTLED_AFTER_DAYS = 3  # Brief §3: each run re-reads the last 2 to 3 days.
LOOKAHEAD_DAYS = 7
REGULAR_SEASON = 2
OTHER = "OTHER"
DEFAULT_DATA_DIR = "/workspace/data"


@dataclass(frozen=True)
class SportSource:
    sport: str
    espn_sport: str  # e.g. football
    espn_league: str  # e.g. college-football
    levels: dict[str, str]  # ESPN group to level. Each group is one scoreboard feed.
    season_starts: tuple[int, int]  # (month, day) in the season's own year.
    season_ends: tuple[int, int]  # (month, day) in the following year.
    limit: str = "500"

    @property
    def scoreboard_url(self) -> str:
        return f"{SITE_ROOT}/{self.espn_sport}/{self.espn_league}/scoreboard"

    def level_list_url(self, season: int, group: str) -> str:
        """The core API puts `leagues` between the sport and the league; the site API doesn't."""
        return (
            f"{CORE_ROOT}/{self.espn_sport}/leagues/{self.espn_league}/seasons/{season}"
            f"/types/{REGULAR_SEASON}/groups/{group}/teams"
        )


SOURCES: dict[str, SportSource] = {
    "NCAAF": SportSource(
        sport="NCAAF",
        espn_sport="football",
        espn_league="college-football",
        levels={"80": "FBS", "81": "FCS"},
        season_starts=(8, 15),
        season_ends=(1, 31),
    ),
}

RESULT_SCHEMA: dict[str, Any] = {
    "sport": pl.String,
    "season": pl.Int64,
    "season_type": pl.Int64,
    "week": pl.Int64,
    "game_id": pl.String,
    "kickoff_utc": pl.Datetime("us", "UTC"),
    "slate_date_et": pl.Date,
    "home_id": pl.String,
    "away_id": pl.String,
    "neutral": pl.Boolean,
    "status": pl.String,
    "state": pl.String,
    "completed": pl.Boolean,
    "home_pts": pl.Int64,
    "away_pts": pl.Int64,
}
TEAM_SCHEMA: dict[str, Any] = {
    "sport": pl.String,
    "season": pl.Int64,
    "team_id": pl.String,
    "display_name": pl.String,
    "short_name": pl.String,
    "abbreviation": pl.String,
    "location": pl.String,
    "mascot": pl.String,
    "conference_id": pl.String,
    "level": pl.String,
}
REJECT_SCHEMA: dict[str, Any] = {"game_id": pl.String, "reason": pl.String}


class IngestError(RuntimeError):
    """The data can't be trusted as it is. The build stops instead of guessing."""


def season_for(day: date) -> int:
    """College football seasons are named by the year they start: January bowls belong to the
    season before."""
    return day.year if day.month >= 7 else day.year - 1


def season_window(source: SportSource, season: int) -> tuple[date, date]:
    return date(season, *source.season_starts), date(season + 1, *source.season_ends)


def raw_dir(data_dir: Path, sport: str) -> Path:
    return data_dir / "raw/espn/v2" / sport


def board_path(data_dir: Path, sport: str, day: date, group: str) -> Path:
    return raw_dir(data_dir, sport) / f"{day:%Y/%m/%d}/scoreboard-{group}.json.gz"


def level_list_path(data_dir: Path, sport: str, season: int, group: str) -> Path:
    return raw_dir(data_dir, sport) / f"registry/{season}-group-{group}-teams.json.gz"


def checkpoint_path(data_dir: Path, sport: str, season: int) -> Path:
    return data_dir / "ingest/checkpoints" / f"{sport}-{season}.json"


def read_checkpoint(data_dir: Path, sport: str, season: int) -> dict[str, Any]:
    path = checkpoint_path(data_dir, sport, season)
    if path.exists():
        checkpoint: dict[str, Any] = json.loads(path.read_text("utf-8"))
        return checkpoint
    return {"settled_dates": [], "failures": {}}


def _write_checkpoint(data_dir: Path, sport: str, season: int, value: dict[str, Any]) -> None:
    path = checkpoint_path(data_dir, sport, season)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True, indent=1), "utf-8")
    os.replace(temporary, path)


def _state(event: dict[str, Any]) -> str:
    status = event["competitions"][0].get("status") or event.get("status") or {}
    return str(status.get("type", {}).get("state", ""))


def is_settled(day: date, boards: Sequence[dict[str, Any]], today: date) -> bool:
    """Old enough, and every game on it has ended (final, postponed or cancelled)."""
    if day > today - timedelta(days=SETTLED_AFTER_DAYS):
        return False
    return all(_state(event) == "post" for board in boards for event in board.get("events", []))


def dates_to_fetch(source: SportSource, season: int, today: date, settled: set[str]) -> list[date]:
    start, end = season_window(source, season)
    last = min(end, today + timedelta(days=LOOKAHEAD_DAYS))
    days = [start + timedelta(days=i) for i in range((last - start).days + 1)]
    return [day for day in days if day.isoformat() not in settled]


def fetch_level_lists(backfill: Backfill, source: SportSource, season: int) -> None:
    """ESPN's per-season team list for each level, fetched once per season."""
    for group in source.levels:
        path = level_list_path(backfill.data_dir, source.sport, season, group)
        if path.exists():
            continue
        url = source.level_list_url(season, group)
        document = fetch_json(backfill, url, {"limit": "1000"})
        if document.get("pageCount", 1) != 1:
            raise IngestError(f"Group {group} team list has more than one page: {url}")
        write_gz(path, document)


def refresh(backfill: Backfill, source: SportSource, season: int) -> dict[str, Any]:
    """Fetch every unsettled date of the season. Returns what was fetched and what failed."""
    assert_not_halted(backfill.data_dir)
    fetch_level_lists(backfill, source, season)
    checkpoint = read_checkpoint(backfill.data_dir, source.sport, season)
    settled = set(checkpoint["settled_dates"])
    failures: dict[str, str] = {}
    fetched: list[str] = []
    for day in dates_to_fetch(source, season, backfill.today_et, settled):
        boards: list[dict[str, Any]] = []
        try:
            for group in source.levels:
                board = fetch_json(
                    backfill,
                    source.scoreboard_url,
                    {"dates": f"{day:%Y%m%d}", "groups": group, "limit": source.limit},
                )
                write_gz(board_path(backfill.data_dir, source.sport, day, group), board)
                boards.append(board)
        except HttpFetchError as error:
            failures[day.isoformat()] = str(error)
            continue
        fetched.append(day.isoformat())
        if is_settled(day, boards, backfill.today_et):
            settled.add(day.isoformat())
    checkpoint = {"settled_dates": sorted(settled), "failures": failures}
    _write_checkpoint(backfill.data_dir, source.sport, season, checkpoint)
    backfill.log(
        f"{source.sport} {season}: fetched {len(fetched)} dates, {len(settled)} settled, "
        f"{len(failures)} failed"
    )
    return {"fetched": fetched, "failures": failures, "settled": len(settled)}


@dataclass
class SeasonBuild:
    results: list[dict[str, Any]] = field(default_factory=list)
    teams: list[dict[str, Any]] = field(default_factory=list)
    rejects: list[dict[str, str]] = field(default_factory=list)


def read_levels(data_dir: Path, source: SportSource, season: int) -> dict[str, str]:
    """Team ID to level. Raises if a list is missing or a team sits on two lists."""
    levels: dict[str, str] = {}
    for group, level in source.levels.items():
        path = level_list_path(data_dir, source.sport, season, group)
        if not path.exists():
            raise IngestError(f"No {level} team list for {source.sport} {season}: run refresh")
        for item in read_gz(path)["items"]:
            team_id = str(item["$ref"]).split("/teams/")[1].split("?")[0]
            if team_id in levels and levels[team_id] != level:
                raise IngestError(f"Team {team_id} is both {levels[team_id]} and {level}")
            levels[team_id] = level
    return levels


def _score(competitor: dict[str, Any]) -> int | None:
    value = to_number(competitor.get("score"))
    return int(value) if value is not None and value == int(value) else None


def parse_event(sport: str, event: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """One scoreboard event to a result row, or a reason it can't be used."""
    competition = event["competitions"][0]
    competitors = competition.get("competitors") or []
    home = next((c for c in competitors if c.get("homeAway") == "home"), None)
    away = next((c for c in competitors if c.get("homeAway") == "away"), None)
    if len(competitors) != 2 or home is None or away is None:
        return None, "no_home_and_away"
    if str(home["id"]) == str(away["id"]):
        return None, "team_appears_twice"
    status = competition.get("status", {}).get("type", {})
    completed = status.get("completed") is True and status.get("name") == "STATUS_FINAL"
    home_pts = away_pts = None
    if completed:
        home_pts, away_pts = _score(home), _score(away)
        if home_pts is None or away_pts is None:
            return None, "score_missing"
        for competitor, points in ((home, home_pts), (away, away_pts)):
            periods = [to_number(line.get("value")) for line in competitor.get("linescores") or []]
            if periods and (None in periods or sum(p or 0 for p in periods) != points):
                return None, "periods_do_not_sum_to_score"
    season = event.get("season", {})
    return {
        "sport": sport,
        "season": int(season["year"]),
        "season_type": int(season.get("type", 0)),
        "week": int(event["week"]["number"]) if event.get("week") else None,
        "game_id": str(event["id"]),
        "kickoff_utc": parse_time(event["date"]),
        "slate_date_et": et_date(event["date"]),
        "home_id": str(home["id"]),
        "away_id": str(away["id"]),
        "neutral": bool(competition.get("neutralSite")),
        "status": str(status.get("name", "")),
        "state": str(status.get("state", "")),
        "completed": completed,
        "home_pts": home_pts,
        "away_pts": away_pts,
    }, None


def _team_row(sport: str, season: int, team: dict[str, Any], level: str) -> dict[str, Any]:
    display, location = str(team.get("displayName") or ""), str(team.get("location") or "")
    return {
        "sport": sport,
        "season": season,
        "team_id": str(team["id"]),
        "display_name": display,
        "short_name": str(team.get("shortDisplayName") or ""),
        "abbreviation": str(team.get("abbreviation") or ""),
        "location": location,
        "mascot": str(team.get("name") or ""),
        "conference_id": str(team["conferenceId"]) if team.get("conferenceId") else None,
        "level": level,
    }


def _same_game(first: dict[str, Any], second: dict[str, Any]) -> bool:
    keys = ("home_id", "away_id", "kickoff_utc", "completed", "home_pts", "away_pts")
    return all(first[key] == second[key] for key in keys)


def build_season(data_dir: Path, source: SportSource, season: int) -> SeasonBuild:
    """Read every stored scoreboard of the season. A game in two feeds must agree with itself."""
    levels = read_levels(data_dir, source, season)
    start, end = season_window(source, season)
    rows: dict[str, dict[str, Any]] = {}
    rejected: dict[str, str] = {}
    teams: dict[str, dict[str, Any]] = {}
    day = start
    while day <= end:
        for group in source.levels:
            path = board_path(data_dir, source.sport, day, group)
            if not path.exists():
                continue
            for event in read_gz(path).get("events", []):
                game_id = str(event["id"])
                for competitor in event["competitions"][0].get("competitors") or []:
                    team = competitor.get("team") or {}
                    if team.get("id"):
                        level = levels.get(str(team["id"]), OTHER)
                        teams[str(team["id"])] = _team_row(source.sport, season, team, level)
                row, reason = parse_event(source.sport, event)
                if reason is not None:
                    rejected[game_id] = reason
                elif row is not None and row["season"] != season:
                    rejected[game_id] = "outside_season"
                elif row is not None and game_id in rows and not _same_game(rows[game_id], row):
                    rejected[game_id] = "feeds_disagree"
                elif row is not None:
                    rows[game_id] = row
        day += timedelta(days=1)
    for game_id in rejected:
        rows.pop(game_id, None)
    return SeasonBuild(
        results=sorted(rows.values(), key=lambda r: (r["kickoff_utc"], r["game_id"])),
        teams=sorted(teams.values(), key=lambda r: r["team_id"]),
        rejects=[{"game_id": g, "reason": r} for g, r in sorted(rejected.items())],
    )


def results_path(data_dir: Path, sport: str, season: int) -> Path:
    return data_dir / f"curated/results/sport={sport}/season={season}/part-0.parquet"


def teams_path(data_dir: Path, sport: str, season: int) -> Path:
    return data_dir / f"curated/registry/sport={sport}/season={season}/teams.parquet"


def rejects_path(data_dir: Path, sport: str, season: int) -> Path:
    return data_dir / f"curated/_rejects/results/sport={sport}/season={season}/rejects.parquet"


def write_season(data_dir: Path, source: SportSource, season: int) -> SeasonBuild:
    build = build_season(data_dir, source, season)
    write_parquet(
        pl.DataFrame(build.results, schema=RESULT_SCHEMA),
        results_path(data_dir, source.sport, season),
    )
    write_parquet(
        pl.DataFrame(build.teams, schema=TEAM_SCHEMA), teams_path(data_dir, source.sport, season)
    )
    write_parquet(
        pl.DataFrame(build.rejects, schema=REJECT_SCHEMA),
        rejects_path(data_dir, source.sport, season),
    )
    return build


def read_results(data_dir: Path, sport: str, season: int) -> pl.DataFrame:
    path = results_path(data_dir, sport, season)
    return pl.read_parquet(path) if path.exists() else pl.DataFrame(schema=RESULT_SCHEMA)


def read_teams(data_dir: Path, sport: str, season: int) -> pl.DataFrame:
    path = teams_path(data_dir, sport, season)
    return pl.read_parquet(path) if path.exists() else pl.DataFrame(schema=TEAM_SCHEMA)


def summary_text(data_dir: Path, sport: str, season: int, today: date) -> str:
    lines = []
    halt = halt_path(data_dir)
    if halt.exists():
        lines.append(f"HALTED: {halt.read_text('utf-8')}")
    checkpoint = read_checkpoint(data_dir, sport, season)
    lines.append(f"{sport} {season}: {len(checkpoint['settled_dates'])} settled dates")
    for day, error in sorted(checkpoint["failures"].items()):
        lines.append(f"  failed {day}: {error}")
    results, teams = read_results(data_dir, sport, season), read_teams(data_dir, sport, season)
    finals = results.filter(pl.col("completed"))
    upcoming = results.filter(pl.col("state") == "pre")
    stale = upcoming.filter(pl.col("slate_date_et") < today - timedelta(days=SETTLED_AFTER_DAYS))
    lines.append(f"  {finals.height} finals, {upcoming.height} not started, {results.height} total")
    if stale.height:
        lines.append(f"  WARNING: {stale.height} games still unplayed after their date")
    levels = Counter(teams["level"].to_list())
    lines.append(f"  teams: {dict(sorted(levels.items()))}")
    path = rejects_path(data_dir, sport, season)
    if path.exists():
        reasons = Counter(pl.read_parquet(path)["reason"].to_list())
        lines.append(f"  rejects: {dict(sorted(reasons.items())) or 'none'}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("refresh", "build", "status"):
        command = commands.add_parser(name)
        command.add_argument("--sport", choices=sorted(SOURCES), required=True)
        command.add_argument("--season", type=int, help="Defaults to the current season.")
    args = parser.parse_args()

    data_dir = Path(os.environ.get("HOOPSEDGE_DATA_DIR", DEFAULT_DATA_DIR))
    today = today_eastern()
    source = SOURCES[args.sport]
    season = args.season or season_for(today)
    outcome: dict[str, Any] = {"failures": {}}
    if args.command == "status":
        print(summary_text(data_dir, source.sport, season, today))
        return 0
    if args.command == "refresh":
        client = PoliteClient(USER_AGENT, rate_per_second=2.0)
        try:
            outcome = refresh(Backfill(data_dir, client, today), source, season)
        except BlockedError as error:
            print(f"HALTED: {error}", file=sys.stderr)
            return 2
        finally:
            client.close()
        if outcome["failures"]:
            print(f"Failed dates: {sorted(outcome['failures'])}", file=sys.stderr)
    build = write_season(data_dir, source, season)
    print(
        f"{source.sport} {season}: {len(build.results)} games, {len(build.teams)} teams, "
        f"{len(build.rejects)} rejects"
    )
    print(summary_text(data_dir, source.sport, season, today))
    return 1 if args.command == "refresh" and outcome["failures"] else 0


if __name__ == "__main__":
    sys.exit(main())
