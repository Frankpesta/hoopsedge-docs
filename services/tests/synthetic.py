"""Synthetic seasons in the curated table shapes, for ratings and leakage tests."""

from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np
import polars as pl

from hoopsedge.common.schemas import PlayerGame, TeamGame
from hoopsedge.ingest.curated import GAME_SCHEMA, SCHEMAS

TEAM_SCHEMA = SCHEMAS[TeamGame]
PLAYER_SCHEMA = SCHEMAS[PlayerGame]
HOME_EDGE = 3.0


def season_frames(
    season: int,
    teams: int = 12,
    days: int = 60,
    games_per_day: int = 4,
    seed: int = 1,
    league: str = "NBA",
    strengths: np.ndarray | None = None,
    start: date | None = None,
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Return (games, team_games). Stronger teams really do outscore weaker ones."""
    rng = np.random.default_rng(seed)
    power = strengths if strengths is not None else rng.normal(0, 6, teams)
    first = start or date(season - 1, 11, 1)
    regulation = 48 if league == "NBA" else 40
    game_rows: list[dict[str, Any]] = []
    team_rows: list[dict[str, Any]] = []
    number = 0
    for day_index in range(days):
        slate = first + timedelta(days=day_index)
        order = rng.permutation(len(power))
        for slot in range(min(games_per_day, len(power) // 2)):
            home, away = int(order[2 * slot]), int(order[2 * slot + 1])
            neutral = bool(rng.random() < 0.05)
            edge = 0.0 if neutral else HOME_EDGE
            poss = float(rng.normal(98, 4))
            home_rate = 110 + power[home] - power[away] + edge / 2 + rng.normal(0, 8)
            away_rate = 110 + power[away] - power[home] - edge / 2 + rng.normal(0, 8)
            home_pts = max(int(home_rate * poss / 100), 50)
            away_pts = max(int(away_rate * poss / 100), 50)
            if home_pts == away_pts:
                home_pts += 1
            home_1h = home_pts // 2 + int(rng.integers(-3, 4))
            away_1h = away_pts // 2 + int(rng.integers(-3, 4))
            game_id = f"{season}{number:05d}"
            number += 1
            tip = datetime(slate.year, slate.month, slate.day, 23, 0, tzinfo=UTC) + timedelta(
                minutes=30 * slot
            )
            game_rows.append(
                {
                    "league": league,
                    "season": season,
                    "game_id": game_id,
                    "tip_utc": tip,
                    "slate_date_et": slate,
                    "home_id": f"T{home:02d}",
                    "away_id": f"T{away:02d}",
                    "neutral": neutral,
                    "venue_id": "1",
                    "status": "STATUS_FINAL",
                    "home_pts": home_pts,
                    "away_pts": away_pts,
                    "home_1h": home_1h,
                    "away_1h": away_1h,
                    "home_periods": [home_1h, home_pts - home_1h],
                    "away_periods": [away_1h, away_pts - away_1h],
                    "ot_periods": 0,
                    "postseason": False,
                    "season_flag": None,
                    "box_missing": False,
                }
            )
            for team, opp, is_home, pts, half in (
                (home, away, True, home_pts, home_1h),
                (away, home, False, away_pts, away_1h),
            ):
                fg3m = int(rng.integers(8, 16))
                ftm = int(rng.integers(10, 22))
                fgm = max((pts - fg3m - ftm) // 2, 1)
                ftm = pts - 2 * fgm - fg3m
                fga = int(fgm / rng.uniform(0.42, 0.52))
                team_rows.append(
                    {
                        "game_id": game_id,
                        "team_id": f"T{team:02d}",
                        "opp_id": f"T{opp:02d}",
                        "is_home": is_home,
                        "fgm": fgm,
                        "fga": fga,
                        "fg3m": fg3m,
                        "fg3a": int(fg3m / rng.uniform(0.3, 0.4)),
                        "ftm": ftm,
                        "fta": int(max(ftm, 1) / rng.uniform(0.7, 0.85)),
                        "oreb": int(rng.integers(6, 14)),
                        "dreb": int(rng.integers(26, 38)),
                        "tov": int(rng.integers(9, 18)),
                        "pf": int(rng.integers(14, 24)),
                        "pts": pts,
                        "pts_1h": half,
                        "minutes": regulation,
                        "poss": poss,
                    }
                )
    return (
        pl.DataFrame(game_rows, schema=GAME_SCHEMA),
        pl.DataFrame(team_rows, schema=TEAM_SCHEMA),
    )


def empty_players() -> pl.DataFrame:
    return pl.DataFrame(schema=PLAYER_SCHEMA)


def empty_injuries() -> pl.DataFrame:
    return pl.DataFrame(
        schema={
            "captured_at": pl.Datetime("us", "UTC"),
            "player_id": pl.String,
            "team_id": pl.String,
            "status": pl.String,
            "detail": pl.String,
        }
    )


def corrupt_from(
    games: pl.DataFrame,
    team_games: pl.DataFrame,
    cutoff: datetime,
    seed: int = 99,
    delete_from: datetime | None = None,
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Scramble results from `cutoff` on, and delete some games, as docs/04 section 3.5 asks.

    Deleting starts at `delete_from`, by default also `cutoff`. A game that tipped before as_of
    has a hidden result but a known tip time, so only its result may be scrambled.
    """
    rng = np.random.default_rng(seed)
    is_future = games["tip_utc"].dt.timestamp("us").to_numpy() >= int(cutoff.timestamp() * 1e6)
    noise = rng.integers(40, 160, (len(games), 4))
    changed = games.with_columns(
        pl.Series("home_pts", np.where(is_future, noise[:, 0], games["home_pts"].to_numpy())),
        pl.Series("away_pts", np.where(is_future, noise[:, 1], games["away_pts"].to_numpy())),
        pl.Series("home_1h", np.where(is_future, noise[:, 2], games["home_1h"].to_numpy())),
        pl.Series("away_1h", np.where(is_future, noise[:, 3], games["away_1h"].to_numpy())),
    )
    dropped = rng.random(len(games)) < 0.25
    deletable = games["tip_utc"].dt.timestamp("us").to_numpy() >= int(
        (delete_from or cutoff).timestamp() * 1e6
    )
    keep = ~(deletable & dropped)
    future_ids = set(games.filter(pl.Series(is_future))["game_id"].to_list())
    box_noise = rng.integers(1, 90, len(team_games))
    in_future = team_games["game_id"].is_in(sorted(future_ids)).to_numpy()
    changed_box = team_games.with_columns(
        pl.Series("pts", np.where(in_future, box_noise, team_games["pts"].to_numpy())),
        pl.Series("fga", np.where(in_future, box_noise + 1, team_games["fga"].to_numpy())),
        pl.Series(
            "poss", np.where(in_future, box_noise.astype(float) + 50, team_games["poss"].to_numpy())
        ),
    )
    remaining = changed.filter(pl.Series(keep))
    return remaining, changed_box.filter(pl.col("game_id").is_in(remaining["game_id"].to_list()))
