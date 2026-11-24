"""A synthetic features table with real signal, for fast model and backtest tests."""

from datetime import UTC, date, datetime, timedelta

import numpy as np
import polars as pl

from hoopsedge.features.registry import FEATURES


def feature_frame(
    games_per_day: int = 8,
    days: int = 150,
    seed: int = 7,
    season: int = 2026,
    start: date = date(2025, 11, 1),
    league: str = "NBA",
) -> pl.DataFrame:
    """Scores really do follow `exp_ppp` and `exp_poss`; every other feature is noise."""
    rng = np.random.default_rng(seed)
    count = games_per_day * days
    columns: dict[str, np.ndarray] = {name: rng.normal(0, 1, count) for name in FEATURES}
    power_home = rng.normal(0, 5, count)
    power_away = rng.normal(0, 5, count)
    exp_poss = rng.normal(98, 3, count)
    columns["exp_poss"] = exp_poss
    columns["exp_ppp_h"] = 112 + power_home - power_away + 1.5
    columns["exp_ppp_a"] = 112 + power_away - power_home - 1.5
    columns["exp_poss_1h"] = exp_poss / 2
    columns["exp_ppp_1h_h"] = columns["exp_ppp_h"]
    columns["exp_ppp_1h_a"] = columns["exp_ppp_a"]
    columns["adj_o_h"] = 112 + power_home
    columns["adj_d_h"] = 112 - power_home
    columns["adj_o_a"] = 112 + power_away
    columns["adj_d_a"] = 112 - power_away
    columns["elo_diff"] = 30 * (power_home - power_away)
    columns["neutral"] = (rng.random(count) < 0.05).astype(float)
    # An injury feature that's always null in history, as D15 expects.
    columns["avail_missing_h"] = np.full(count, np.nan)

    mean_home = columns["exp_ppp_h"] * exp_poss / 100
    mean_away = columns["exp_ppp_a"] * exp_poss / 100
    home = np.rint(rng.normal(mean_home, 11)).astype(int)
    away = np.rint(rng.normal(mean_away, 11)).astype(int)
    home_1h = np.rint(home * 0.5 + rng.normal(0, 4, count)).astype(int)
    away_1h = np.rint(away * 0.5 + rng.normal(0, 4, count)).astype(int)

    slates = [start + timedelta(days=i // games_per_day) for i in range(count)]
    tips = [datetime(d.year, d.month, d.day, 23, tzinfo=UTC) for d in slates]
    return pl.DataFrame(
        {
            "game_id": [f"{season}{i:06d}" for i in range(count)],
            "league": [league] * count,
            "season": [season] * count,
            "slate_date_et": slates,
            "tip_utc": tips,
            "season_flag": [None] * count,
            **{name: values.astype(float) for name, values in columns.items()},
            "target_home_pts": home,
            "target_away_pts": away,
            "target_home_1h": home_1h,
            "target_away_1h": away_1h,
        },
        schema_overrides={"season_flag": pl.String},
    ).with_columns(pl.col(name).fill_nan(None) for name in FEATURES)
