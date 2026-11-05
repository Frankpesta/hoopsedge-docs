"""Training sets, ratings snapshots and the sanity report (docs/04 sections 3.4 and DoD).

    docker compose run --rm dev python -m hoopsedge.features.training build --league NBA
    docker compose run --rm dev python -m hoopsedge.features.training sanity
    docker compose run --rm dev python -m hoopsedge.features.training leakage --league NBA

D14: replay uses each slate's scheduled run time as `as_of`, so training sees just what a live
run would have seen. A game that tips before the run uses its tip time instead.
"""

import argparse
import json
import os
import random
import sys
import time
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from datetime import time as clock
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import polars as pl

from hoopsedge.features.builder import features_for_game
from hoopsedge.features.registry import FEATURE_SET_VERSION, FEATURES
from hoopsedge.ingest.backfill import FIRST_SEASON, current_season
from hoopsedge.ingest.curated import write_parquet
from hoopsedge.ingest.espn_coverage import EASTERN
from hoopsedge.ratings.engine import Engine, RatingsSnapshot
from hoopsedge.ratings.history import GameRef, History
from hoopsedge.teams.registry import NON_D1

LAGOS = ZoneInfo("Africa/Lagos")
# D6's defaults. The config item can move these later, and history keeps these values.
SLATE_RUN_TIMES = {"NCAA": clock(15, 0), "NBA": clock(19, 30)}
TARGET_CORRELATION = {"NBA": 0.85, "NCAA": 0.8}
TOP_TEAMS = 25
MIN_TEAM_GAMES_FOR_SANITY = {"NBA": 40, "NCAA": 15}
LEAKAGE_SAMPLE = 500
DEFAULT_DATA_DIR = "/workspace/data"
DEFAULT_REPORT_DIR = "/workspace/docs/reports"
Log = Callable[[str], None]


def slate_run_time(league: str, slate: date) -> datetime:
    return datetime.combine(slate, SLATE_RUN_TIMES[league], tzinfo=LAGOS).astimezone(UTC)


def ref_from_row(row: dict[str, Any]) -> GameRef:
    return GameRef(
        league=row["league"],
        season=row["season"],
        game_id=row["game_id"],
        tip_utc=row["tip_utc"],
        home_id=row["home_id"],
        away_id=row["away_id"],
        neutral=row["neutral"],
        postseason=row["postseason"],
        slate_date_et=row["slate_date_et"],
    )


def _snapshot_frame(snap: RatingsSnapshot, as_of: datetime) -> pl.DataFrame:
    rows = {
        "team_id": snap.team_ids,
        "games": [snap.games_played(team) for team in snap.team_ids],
        **{name: values.tolist() for name, values in snap.values.items()},
    }
    frame = pl.DataFrame(rows, schema_overrides={"team_id": pl.String, "games": pl.Int64})
    return frame.with_columns(
        pl.lit(as_of).alias("as_of"),
        pl.lit(snap.league["mu"]).alias("mu"),
        pl.lit(snap.league["tau"]).alias("tau"),
        pl.lit(snap.league["hca"]).alias("hca"),
    )


def _features_path(data_dir: Path, league: str, season: int) -> Path:
    return data_dir / f"curated/features/league={league}/season={season}.parquet"


def _row_for(ref: GameRef, row: dict[str, Any], engine: Engine, as_of: datetime) -> dict[str, Any]:
    built = features_for_game(engine, ref, as_of)
    return {
        "game_id": built.game_id,
        "league": ref.league,
        "season": ref.season,
        "slate_date_et": ref.slate_date_et,
        "tip_utc": ref.tip_utc,
        "as_of": built.as_of,
        "feature_set_version": built.feature_set_version,
        "feature_hash": built.feature_hash,
        **built.features,
        "target_home_pts": row["home_pts"],
        "target_away_pts": row["away_pts"],
        "target_home_1h": row["home_1h"],
        "target_away_1h": row["away_1h"],
        "target_ot": 1 if row["ot_periods"] else 0,
    }


def _feature_schema() -> dict[str, Any]:
    return {
        "game_id": pl.String,
        "league": pl.String,
        "season": pl.Int64,
        "slate_date_et": pl.Date,
        "tip_utc": pl.Datetime("us", "UTC"),
        "as_of": pl.Datetime("us", "UTC"),
        "feature_set_version": pl.String,
        "feature_hash": pl.String,
        **{name: pl.Float64 for name in FEATURES},
        "target_home_pts": pl.Int64,
        "target_away_pts": pl.Int64,
        "target_home_1h": pl.Int64,
        "target_away_1h": pl.Int64,
        "target_ot": pl.Int64,
    }


def build_training_set(
    data_dir: Path, league: str, seasons: Sequence[int], log: Log = print
) -> dict[int, dict[str, Any]]:
    """Replay each season by slate, appending only games not already built at this version."""
    history = History.load(data_dir, league, sorted(seasons))
    engine = Engine(history)
    summary: dict[int, dict[str, Any]] = {}
    available = set(history.seasons())
    for season in sorted(seasons):
        if season not in available:
            continue
        started = time.monotonic()
        path = _features_path(data_dir, league, season)
        existing = pl.DataFrame()
        known: set[str] = set()
        if path.exists():
            existing = pl.read_parquet(path)
            existing = existing.filter(pl.col("feature_set_version") == FEATURE_SET_VERSION)
            known = set(existing["game_id"].to_list())
        games = history.games_frame(season)
        new_rows: list[dict[str, Any]] = []
        snapshots = 0
        for (slate,), group in games.group_by(["slate_date_et"], maintain_order=True):
            pending = [row for row in group.to_dicts() if row["game_id"] not in known]
            if not pending:
                continue
            run = slate_run_time(league, slate)
            for row in pending:
                ref = ref_from_row(row)
                new_rows.append(_row_for(ref, row, engine, min(run, ref.tip_utc)))
            snapshot = engine.ratings_as_of(season, run)
            write_parquet(
                _snapshot_frame(snapshot, run),
                data_dir
                / f"curated/ratings_snapshots/league={league}/season={season}/{slate}.parquet",
            )
            snapshots += 1
        added = pl.DataFrame(new_rows, schema=_feature_schema()) if new_rows else None
        if added is not None:
            frame = pl.concat([existing, added]) if len(existing) else added
            write_parquet(frame.sort(["tip_utc", "game_id"]), path)
        rows = len(existing) + len(new_rows)
        seconds = round(time.monotonic() - started, 1)
        summary[season] = {
            "rows": rows,
            "new_rows": len(new_rows),
            "snapshots": snapshots,
            "seconds": seconds,
        }
        log(f"{league} {season}: {rows} rows, {len(new_rows)} new, {snapshots} slates, {seconds} s")
    return summary


def sanity_rows(history: History, season: int) -> dict[str, Any]:
    """End-of-season ratings against win percentage, and the top teams."""
    engine = Engine(history)
    snap = engine.season_final(season)
    games = history.games_frame(season)
    wins: dict[str, int] = {}
    played: dict[str, int] = {}
    for row in games.to_dicts():
        home = history.pooled_team(season, row["home_id"])
        away = history.pooled_team(season, row["away_id"])
        played[home] = played.get(home, 0) + 1
        played[away] = played.get(away, 0) + 1
        winner = home if row["home_pts"] > row["away_pts"] else away
        wins[winner] = wins.get(winner, 0) + 1
    minimum = MIN_TEAM_GAMES_FOR_SANITY[history.league]
    teams = [t for t in snap.team_ids if t != NON_D1 and played.get(t, 0) >= minimum]
    net = np.array([snap.value("off", t) + snap.value("def", t) for t in teams])
    win_pct = np.array([wins.get(t, 0) / played[t] for t in teams])
    correlation = float(np.corrcoef(net, win_pct)[0, 1]) if len(teams) > 2 else None
    elo = np.array([snap.value("elo", t) for t in teams])
    order = np.argsort(-net)[:TOP_TEAMS]
    return {
        "teams": len(teams),
        "games": len(games),
        "correlation_net_win_pct": None if correlation is None else round(correlation, 4),
        "correlation_elo_win_pct": round(float(np.corrcoef(elo, win_pct)[0, 1]), 4)
        if len(teams) > 2
        else None,
        "league_mu": round(snap.league["mu"], 2),
        "league_tau": round(snap.league["tau"], 2),
        "league_hca": round(snap.league["hca"], 2),
        "top": [
            {
                "team_id": teams[i],
                "net": round(float(net[i]), 2),
                "win_pct": round(float(win_pct[i]), 3),
            }
            for i in order
        ],
    }


def _team_names(data_dir: Path) -> dict[tuple[str, str], str]:
    path = data_dir / "curated/teams/teams.parquet"
    if not path.exists():
        return {}
    return {
        (row["league"], row["team_id"]): row["full_name"]  # D42: IDs repeat across leagues.
        for row in pl.read_parquet(path).to_dicts()
    }


def write_sanity_report(
    data_dir: Path, report_dir: Path, today: date, log: Log = print
) -> dict[str, Any]:
    results: dict[str, Any] = {"generated_at": datetime.now(UTC).isoformat()}
    names = _team_names(data_dir)
    lines = [
        "# Phase 3: Ratings Sanity",
        "",
        f"Generated by `hoopsedge.features.training sanity` on {results['generated_at'][:10]}. "
        "Each correlation compares end-of-season net rating with win percentage, for teams with "
        "enough games. The current season and partly downloaded seasons are shown for "
        "information only.",
    ]
    for league, target in TARGET_CORRELATION.items():
        seasons = list(range(FIRST_SEASON[league], current_season(today) + 1))
        history = History.load(data_dir, league, seasons)
        lines += [
            "",
            f"## {league}",
            "",
            f"The target correlation is above {target}.",
            "",
            "| Season | Games | Teams | Net vs win % | Elo vs win % | League PPP | Tempo "
            "| Home edge | Result |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        results[league] = {}
        for season in history.seasons():
            row = sanity_rows(history, season)
            results[league][str(season)] = row
            value = row["correlation_net_win_pct"]
            if value is None:
                verdict = "too few games"
            else:
                verdict = "met" if value > target else "BELOW TARGET"
            lines.append(
                f"| {season} | {row['games']} | {row['teams']} | {value} "
                f"| {row['correlation_elo_win_pct']} | {row['league_mu']} | {row['league_tau']} "
                f"| {row['league_hca']} | {verdict} |"
            )
            log(f"{league} {season}: correlation {value}")
        if results[league]:
            newest_complete = [int(s) for s in results[league] if int(s) < current_season(today)]
            if newest_complete:
                season = max(newest_complete)
                lines += ["", f"### Top {TOP_TEAMS}, {season}", ""]
                lines += ["| Rank | Team | Net per 100 | Win % |", "|---|---|---|---|"]
                for rank, team in enumerate(results[league][str(season)]["top"], 1):
                    name = names.get((league, team["team_id"]), team["team_id"])
                    lines.append(f"| {rank} | {name} | {team['net']} | {team['win_pct']} |")
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "phase3-ratings-sanity.json").write_text(
        json.dumps(results, indent=1, sort_keys=True) + "\n", "utf-8"
    )
    (report_dir / "phase3-ratings-sanity.md").write_text("\n".join(lines) + "\n", "utf-8")
    return results


def real_leakage_check(
    data_dir: Path, league: str, season: int, sample: int = LEAKAGE_SAMPLE, log: Log = print
) -> dict[str, Any]:
    """The mutation test on real games. Each check truncates the season at the game's cutoff."""
    history = History.load(data_dir, league, [season - 1, season])
    games = history.games_frame(season)
    rows = games.to_dicts()
    rng = random.Random(20261105)  # noqa: S311 - Sampling a check, not security.
    picked = [rng.choice(rows) for _ in range(min(sample, len(rows)))]
    clean = Engine(history)
    failures: list[str] = []
    all_games = history._games  # noqa: SLF001 - This check exists to rebuild a history.
    all_box = history._team_games  # noqa: SLF001
    for row in picked:
        ref = ref_from_row(row)
        as_of = min(slate_run_time(league, ref.slate_date_et), ref.tip_utc)
        before = features_for_game(clean, ref, as_of)
        cutoff = ref.tip_utc
        kept = all_games.filter(pl.col("tip_utc") < cutoff)
        truncated = History(
            league,
            kept,
            all_box.filter(pl.col("game_id").is_in(kept["game_id"].to_list())),
            history._players,  # noqa: SLF001
            history._injuries,  # noqa: SLF001
            history._d1,  # noqa: SLF001
        )
        after = features_for_game(Engine(truncated), ref, as_of)
        if before.feature_hash != after.feature_hash:
            failures.append(ref.game_id)
    log(f"{league} {season}: {len(picked)} games checked, {len(failures)} changed")
    return {"checked": len(picked), "changed": failures}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build")
    build.add_argument("--league", choices=sorted(TARGET_CORRELATION))
    build.add_argument("--seasons", nargs="*", type=int)
    commands.add_parser("sanity")
    leakage = commands.add_parser("leakage")
    leakage.add_argument("--league", choices=sorted(TARGET_CORRELATION), required=True)
    leakage.add_argument("--season", type=int)
    args = parser.parse_args()

    data_dir = Path(os.environ.get("HOOPSEDGE_DATA_DIR", DEFAULT_DATA_DIR))
    today = datetime.now(UTC).astimezone(EASTERN).date()
    if args.command == "sanity":
        report_dir = Path(os.environ.get("HOOPSEDGE_REPORT_DIR", DEFAULT_REPORT_DIR))
        write_sanity_report(data_dir, report_dir, today)
        return 0
    if args.command == "leakage":
        season = args.season or current_season(today) - 1
        result = real_leakage_check(data_dir, args.league, season)
        print(json.dumps(result))
        return 1 if result["changed"] else 0
    for league in [args.league] if args.league else list(TARGET_CORRELATION):
        seasons = args.seasons or list(range(FIRST_SEASON[league], current_season(today) + 1))
        build_training_set(data_dir, league, seasons)
    return 0


if __name__ == "__main__":
    sys.exit(main())
