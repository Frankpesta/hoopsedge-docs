"""The three baselines every model must beat (docs/05 section 4.4, D17)."""

import numpy as np
import polars as pl
import pytest

from hoopsedge.models.baselines import elo_baseline, prior_only_columns, ratings_baseline
from hoopsedge.ratings.engine import Engine
from hoopsedge.ratings.history import History
from tests.model_data import feature_frame
from tests.synthetic import empty_injuries, empty_players, season_frames


def test_ratings_baseline_is_rate_times_possessions() -> None:
    frame = feature_frame(days=30)
    dist = ratings_baseline(frame, frame, "ft")
    row = frame.row(0, named=True)
    assert dist.mu_home[0] == pytest.approx(row["exp_ppp_h"] * row["exp_poss"] / 100)
    assert dist.mu_away[0] == pytest.approx(row["exp_ppp_a"] * row["exp_poss"] / 100)


def test_ratings_baseline_first_half_uses_first_half_inputs() -> None:
    frame = feature_frame(days=30)
    dist = ratings_baseline(frame, frame, "h1")
    row = frame.row(0, named=True)
    assert dist.mu_home[0] == pytest.approx(row["exp_ppp_1h_h"] * row["exp_poss_1h"] / 100)


def test_baseline_spread_is_one_constant_from_the_training_rows_only() -> None:
    frame = feature_frame(days=60)
    train, test = frame[:240], frame[240:]
    dist = ratings_baseline(train, test, "ft")
    assert len(np.unique(dist.sd_margin)) == 1  # D17: the league constant.
    scrambled = test.with_columns(pl.col("target_home_pts") + 500)
    assert ratings_baseline(train, scrambled, "ft").sd_margin[0] == dist.sd_margin[0]


def test_elo_baseline_learns_the_points_per_elo_and_uses_a_flat_total() -> None:
    frame = feature_frame(days=120)
    dist = elo_baseline(frame, frame, "ft")
    assert len(np.unique(np.round(dist.mu_total, 6))) == 1
    margin = (frame["target_home_pts"] - frame["target_away_pts"]).to_numpy()
    assert np.corrcoef(dist.mu_margin, margin)[0, 1] > 0.3


def _history() -> History:
    frames = [season_frames(season, seed=season) for season in (2025, 2026)]
    games = pl.concat([f[0] for f in frames])
    box = pl.concat([f[1] for f in frames])
    return History("NBA", games, box, empty_players(), empty_injuries(), {})


def test_prior_only_uses_no_game_from_the_season_it_predicts() -> None:
    history = _history()
    before = prior_only_columns(Engine(history), 2026)

    games, box = history._games, history._team_games  # noqa: SLF001
    scrambled = games.with_columns(
        pl.when(pl.col("season") == 2026).then(200).otherwise(pl.col("home_pts")).alias("home_pts")
    )
    changed = History("NBA", scrambled, box, empty_players(), empty_injuries(), {})
    after = prior_only_columns(Engine(changed), 2026)
    assert before["prior_home_ft"].to_list() == after["prior_home_ft"].to_list()
    assert before["prior_away_h1"].to_list() == after["prior_away_h1"].to_list()


def test_prior_only_is_null_when_there_is_no_previous_season() -> None:
    frame = prior_only_columns(Engine(_history()), 2025)
    assert frame["prior_home_ft"].null_count() == len(frame)
