"""The ratings grid must score on pre-game knowledge and must not change the shared config."""

import polars as pl

from hoopsedge.backtest.ratings_grid import Setting, grid, priced_d1_mask, replay, summarize
from hoopsedge.ratings.engine import CONFIG
from hoopsedge.ratings.history import History
from tests.synthetic import empty_injuries, empty_players, season_frames


def _history() -> History:
    frames = [season_frames(season, seed=season) for season in (2025, 2026)]
    return History(
        "NBA",
        pl.concat([f[0] for f in frames]),
        pl.concat([f[1] for f in frames]),
        empty_players(),
        empty_injuries(),
        {},
    )


SETTING = Setting(r=0.2, k=4.0, hca_k=50.0)


def test_a_settings_change_reaches_the_prediction() -> None:
    history = _history()
    first = replay(history, SETTING, 2026)["expected_margin"]
    second = replay(history, Setting(r=0.9, k=30.0, hca_k=500.0), 2026)["expected_margin"]
    assert (first != second).any()


def test_grid_leaves_the_shared_config_alone() -> None:
    before = dict(CONFIG["NBA"])
    grid(_history(), [SETTING], [2026])
    assert CONFIG["NBA"] == before


def test_later_results_cannot_change_an_earlier_expectation() -> None:
    history = _history()
    clean = replay(history, SETTING, 2026)
    games, box = history._games, history._team_games  # noqa: SLF001
    last = games.filter(pl.col("season") == 2026)["tip_utc"].max()
    changed_games = games.with_columns(
        pl.when(pl.col("tip_utc") == last).then(180).otherwise(pl.col("home_pts")).alias("home_pts")
    )
    changed = replay(
        History("NBA", changed_games, box, empty_players(), empty_injuries(), {}), SETTING, 2026
    )
    # The last slate's games can't see their own results.
    assert (clean["expected_margin"] == changed["expected_margin"]).all()


def test_priced_mask_needs_a_line_and_two_d1_teams() -> None:
    frames = season_frames(2026, seed=2026, league="NCAA")
    games = frames[0]
    first, second = games.row(0, named=True), games.row(1, named=True)
    d1 = {first["home_id"], first["away_id"]}
    history = History("NCAA", games, frames[1], empty_players(), empty_injuries(), {2026: d1})
    mask = priced_d1_mask(history, 2026, {first["game_id"], second["game_id"]})
    ordered = history.games_frame(2026)["game_id"].to_list()
    assert mask[ordered.index(first["game_id"])]
    if not {second["home_id"], second["away_id"]} <= d1:
        assert not mask[ordered.index(second["game_id"])]
    assert mask.sum() <= 2


def test_summary_slope_is_one_when_expectations_are_right() -> None:
    import numpy as np

    expected = np.linspace(-20, 20, 50)
    result = {
        "days": np.zeros(50),
        "expected_margin": expected,
        "actual_margin": expected,
        "expected_total": np.full(50, 200.0),
        "actual_total": np.full(50, 200.0),
    }
    summary = summarize(result, 0, 13)
    assert summary["slope"] == 1.0
    assert summary["margin_mae"] == 0.0
