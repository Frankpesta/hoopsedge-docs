"""The walk-forward backtest: no future information, one shared line set (docs/05, D14, D17)."""

from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from hoopsedge.backtest.markets import (
    BASELINE,
    FAMILIES,
    MODEL,
    REAL,
    SYNTHETIC,
    score_markets,
    with_calibration,
)
from hoopsedge.backtest.metrics import (
    bootstrap_difference,
    gate_decision,
    log_loss,
    sweep_thresholds,
)
from hoopsedge.backtest.walkforward import (
    Window,
    known_before,
    run_walkforward,
    season_windows,
)
from hoopsedge.models.training import DEFAULT_PARAMS
from tests.model_data import feature_frame

FAST = {
    **DEFAULT_PARAMS,
    "mean": {**DEFAULT_PARAMS["mean"], "n_estimators": 20},
    "var": {**DEFAULT_PARAMS["var"], "n_estimators": 10},
}
START = date(2025, 11, 1)


def _frame() -> pl.DataFrame:
    frame = feature_frame(days=90).with_columns(
        pl.col("target_home_pts").cast(pl.Float64).alias("prior_home_ft"),
        pl.col("target_away_pts").cast(pl.Float64).alias("prior_away_ft"),
        pl.col("target_home_1h").cast(pl.Float64).alias("prior_home_h1"),
        pl.col("target_away_1h").cast(pl.Float64).alias("prior_away_h1"),
    )
    # A prior-only baseline must not depend on results; here it's a flat constant.
    return frame.with_columns(
        pl.lit(110.0).alias("prior_home_ft"),
        pl.lit(106.0).alias("prior_away_ft"),
        pl.lit(55.0).alias("prior_home_h1"),
        pl.lit(53.0).alias("prior_away_h1"),
    )


WINDOWS = [
    Window(START + timedelta(days=60), START + timedelta(days=75)),
    Window(START + timedelta(days=75), START + timedelta(days=90)),
]


def _predictions() -> pl.DataFrame:
    return run_walkforward(_frame(), WINDOWS, FAST, "NBA")


def test_training_rows_are_only_results_known_at_the_run_time() -> None:
    frame = _frame()
    window_start = WINDOWS[0].start
    kept = known_before(frame, "NBA", window_start)
    assert max(kept["slate_date_et"].to_list()) < window_start


def test_a_late_game_the_night_before_is_only_used_if_its_result_was_known() -> None:
    # D14: tip + 3 h must fall before the slate's run time, 18:30 UTC for the NBA.
    frame = _frame().with_columns(
        pl.when(pl.col("game_id") == pl.first("game_id"))
        .then(pl.datetime(2025, 12, 30, 16, 0, time_zone="UTC"))
        .otherwise(pl.col("tip_utc"))
        .alias("tip_utc"),
    )
    kept = known_before(frame, "NBA", date(2025, 12, 30))
    assert frame["game_id"][0] not in kept["game_id"].to_list()  # Tipped 16:00, known 19:00.


def test_later_results_cannot_change_earlier_predictions() -> None:
    frame = _frame()
    first_end = WINDOWS[0].end
    scrambled = frame.with_columns(
        pl.when(pl.col("slate_date_et") >= first_end)
        .then(pl.col(name) + 37)
        .otherwise(pl.col(name))
        .alias(name)
        for name in (
            "target_home_pts",
            "target_away_pts",
            "target_home_1h",
            "target_away_1h",
        )
    )
    clean = run_walkforward(frame, WINDOWS[:1], FAST, "NBA")
    changed = run_walkforward(scrambled, WINDOWS[:1], FAST, "NBA")
    assert clean["mu_home"].to_list() == changed["mu_home"].to_list()
    assert clean["sd_total"].to_list() == changed["sd_total"].to_list()


def test_results_in_the_window_being_predicted_cannot_change_its_predictions() -> None:
    frame = _frame()
    window = WINDOWS[0]
    scrambled = frame.with_columns(
        pl.when(pl.col("slate_date_et") >= window.start)
        .then(pl.col("target_home_pts") + 50)
        .otherwise(pl.col("target_home_pts"))
        .alias("target_home_pts"),
    )
    columns = ["game_id", "model", "period", "mu_home", "sd_margin"]
    clean = run_walkforward(frame, [window], FAST, "NBA").select(columns)
    changed = run_walkforward(scrambled, [window], FAST, "NBA").select(columns)
    assert clean.equals(changed)


def test_every_candidate_and_period_predicts_every_test_game() -> None:
    predictions = _predictions()
    test_games = _frame().filter(pl.col("slate_date_et") >= WINDOWS[0].start).height
    counts = predictions.group_by(["model", "period"]).len()
    assert set(counts["len"].to_list()) == {test_games}
    assert set(predictions["model"].unique().to_list()) == {"model", "baseline", "elo", "prior"}
    assert set(predictions["period"].unique().to_list()) == {"ft", "h1"}


def test_no_training_rows_is_an_error() -> None:
    with pytest.raises(ValueError, match="training"):
        run_walkforward(_frame(), [Window(START, START + timedelta(days=5))], FAST, "NBA")


def test_season_windows_cover_the_range_without_overlap_or_gaps() -> None:
    windows = season_windows(date(2025, 11, 1), date(2026, 1, 15), 30)
    assert windows[0].start == date(2025, 11, 1)
    assert windows[-1].end == date(2026, 1, 16)
    for earlier, later in zip(windows, windows[1:], strict=False):
        assert earlier.end == later.start


def _lines(predictions: pl.DataFrame) -> pl.DataFrame:
    ids = predictions["game_id"].unique().sort().to_list()
    return pl.DataFrame(
        {
            "game_id": ids,
            "kind": ["close"] * len(ids),
            "spread_home": [-3.5] * len(ids),
            "total": [218.5] * len(ids),
            "price_home": [1.91] * len(ids),
            "price_away": [1.91] * len(ids),
            "price_over": [1.91] * len(ids),
            "price_under": [1.91] * len(ids),
        }
    )


def test_all_candidates_are_scored_on_the_same_lines() -> None:
    scored = score_markets(_predictions(), _lines(_predictions()))
    for keys, group in scored.group_by(["family", "source"]):
        by_model = {
            name: sub.sort(["game_id", "selection"]) for (name,), sub in group.group_by(["model"])
        }
        reference = by_model[BASELINE].select(["game_id", "selection", "line"])
        for name, sub in by_model.items():
            assert sub.select(["game_id", "selection", "line"]).equals(reference), (keys, name)


def test_real_lines_for_full_game_markets_and_synthetic_everywhere_else() -> None:
    scored = score_markets(_predictions(), _lines(_predictions()))
    real = scored.filter(pl.col("source") == REAL)
    assert set(real["family"].unique().to_list()) == {"handicap", "total_ft"}
    synthetic = scored.filter(pl.col("source") == SYNTHETIC)
    assert set(synthetic["family"].unique().to_list()) == set(FAMILIES)


def test_synthetic_lines_come_from_the_baseline_not_each_model() -> None:
    predictions = _predictions()
    scored = score_markets(predictions, _lines(predictions)).filter(
        (pl.col("source") == SYNTHETIC) & (pl.col("family") == "total_ft")
    )
    game = scored["game_id"][0]
    base = predictions.filter(
        (pl.col("game_id") == game) & (pl.col("model") == BASELINE) & (pl.col("period") == "ft")
    ).row(0, named=True)
    centre = round((base["mu_home"] + base["mu_away"]) * 2) / 2
    lines = sorted(
        scored.filter((pl.col("game_id") == game) & (pl.col("model") == MODEL))["line"]
        .unique()
        .to_list()
    )
    assert lines == [centre - 5, centre - 2.5, centre, centre + 2.5, centre + 5]


def test_outcomes_follow_the_actual_score_and_pushes_are_left_out_of_the_loss() -> None:
    predictions = _predictions()
    game = predictions.filter(pl.col("period") == "ft")["game_id"][0]
    row = predictions.filter((pl.col("game_id") == game) & (pl.col("period") == "ft")).row(
        0, named=True
    )
    margin = row["actual_home"] - row["actual_away"]
    lines = _lines(predictions).with_columns(
        pl.when(pl.col("game_id") == game)
        .then(float(-margin))
        .otherwise(pl.col("spread_home"))
        .alias("spread_home")
    )
    scored = score_markets(predictions, lines).filter(
        (pl.col("game_id") == game) & (pl.col("family") == "handicap") & (pl.col("source") == REAL)
    )
    assert scored["outcome"].is_null().all()  # Exactly on the number: a push.


def test_first_half_totals_settle_on_the_first_half_score() -> None:
    predictions = _predictions().filter(pl.col("period") == "h1")
    full = _predictions().filter(pl.col("period") == "ft")
    scored = score_markets(_predictions(), _lines(full)).filter(
        (pl.col("family") == "total_1h") & (pl.col("model") == MODEL)
    )
    row = scored.filter(pl.col("line") % 1 != 0).row(0, named=True)
    game = predictions.filter(pl.col("game_id") == row["game_id"]).row(0, named=True)
    actual = game["actual_home"] + game["actual_away"]
    assert row["outcome"] == (1.0 if actual > row["line"] else 0.0)


def test_calibration_is_only_ever_fitted_on_earlier_slates() -> None:
    predictions = _predictions()
    scored = score_markets(predictions, _lines(predictions))
    split = WINDOWS[1].start
    clean = with_calibration(scored, split)
    scrambled = scored.with_columns(
        pl.when(pl.col("slate_date_et") >= split)
        .then(1.0 - pl.col("outcome"))
        .otherwise(pl.col("outcome"))
        .alias("outcome")
    )
    changed = with_calibration(scrambled, split)
    keys = ["game_id", "model", "family", "source", "selection", "line"]
    joined = clean.sort(keys)["p_calibrated"].to_list()
    assert joined == changed.sort(keys)["p_calibrated"].to_list()
    # Only rows from the split onward are evaluated.
    assert min(clean["slate_date_et"].to_list()) >= split


def test_log_loss_of_known_values() -> None:
    assert log_loss(np.array([0.5]), np.array([1.0]))[0] == pytest.approx(np.log(2))


def test_bootstrap_interval_excludes_zero_only_for_a_real_difference() -> None:
    rng = np.random.default_rng(1)
    games = np.arange(2000)
    noise = rng.normal(0, 0.05, 2000)
    clear = bootstrap_difference(games, noise + 0.02, seed=1)
    assert clear.low > 0
    none = bootstrap_difference(games, noise - noise.mean(), seed=1)
    assert none.low < 0 < none.high


def test_bootstrap_resamples_whole_games() -> None:
    # Ten rows from one game are one observation, so the interval must stay wide.
    games = np.repeat(np.arange(10), 10)
    values = np.repeat(np.arange(10, dtype=float), 10)
    result = bootstrap_difference(games, values, seed=1)
    assert result.count == 10
    assert result.high - result.low > 2


def test_gate_rule_needs_the_interval_above_zero() -> None:
    assert gate_decision(low=0.001, high=0.01) is True
    assert gate_decision(low=-0.001, high=0.01) is False
    assert gate_decision(low=-0.01, high=-0.001) is False


def test_threshold_sweep_respects_the_legs_per_slate_floor() -> None:
    rng = np.random.default_rng(2)
    count = 3000
    p = rng.uniform(0.45, 0.7, count)
    frame = pl.DataFrame(
        {
            "game_id": [str(i) for i in range(count)],
            "slate_date_et": [START + timedelta(days=i % 60) for i in range(count)],
            "p_calibrated": p,
            "edge": p - 0.5,
            "outcome": (rng.random(count) < p).astype(float),
        }
    )
    chosen = sweep_thresholds(frame, min_legs_per_slate=5)
    assert chosen is not None
    assert 0.52 <= chosen.min_prob <= 0.65
    assert 0.0 <= chosen.min_edge <= 0.05
    assert chosen.legs_per_slate >= 5


def test_threshold_sweep_returns_nothing_when_no_setting_gives_enough_legs() -> None:
    frame = pl.DataFrame(
        {
            "game_id": ["1", "2"],
            "slate_date_et": [START, START],
            "p_calibrated": [0.6, 0.6],
            "edge": [0.1, 0.1],
            "outcome": [1.0, 0.0],
        }
    )
    assert sweep_thresholds(frame, min_legs_per_slate=50) is None
