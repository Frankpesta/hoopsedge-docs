"""LightGBM families: determinism, constraints, spreads and the registry (docs/05, docs/08)."""

from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from hoopsedge.features.registry import UNTRAINED_FEATURES
from hoopsedge.models.distribution import crps_normal
from hoopsedge.models.registry import load_bundle, model_version, save_bundle
from hoopsedge.models.training import (
    DEFAULT_PARAMS,
    TRAIN_FEATURES,
    ModelBundle,
    fit_bundle,
    sample_weights,
    training_data_hash,
)
from tests.model_data import feature_frame

SMALL = {
    **DEFAULT_PARAMS,
    "mean": {**DEFAULT_PARAMS["mean"], "n_estimators": 40},
    "var": {**DEFAULT_PARAMS["var"], "n_estimators": 20},
}


def _bundle(frame: pl.DataFrame, **changes: object) -> ModelBundle:
    return fit_bundle(frame, {**SMALL, **changes})


def test_training_is_deterministic_with_a_fixed_seed() -> None:
    frame = feature_frame(days=40)
    assert _bundle(frame).content_hash() == _bundle(frame).content_hash()


def test_a_different_seed_changes_the_artifact() -> None:
    frame = feature_frame(days=40)
    assert _bundle(frame).content_hash() != _bundle(frame, seed=99).content_hash()


def test_d15_injury_features_never_reach_a_model() -> None:
    assert not set(UNTRAINED_FEATURES) & set(TRAIN_FEATURES)


def test_booster_inputs_match_the_declared_list() -> None:
    bundle = _bundle(feature_frame(days=40))
    assert bundle.ft.mean["home"].feature_name() == list(TRAIN_FEATURES)


def test_monotone_constraints_hold_for_own_offence_and_opponent_defence() -> None:
    frame = feature_frame(days=120)
    bundle = _bundle(frame)
    base = frame[:400]
    better_offence = base.with_columns(pl.col("adj_o_h") + 15)
    worse_defence = base.with_columns(pl.col("adj_d_a") + 15)
    plain = bundle.predict(base, "ft").mu_home
    assert np.all(bundle.predict(better_offence, "ft").mu_home >= plain - 1e-9)
    assert np.all(bundle.predict(worse_defence, "ft").mu_home >= plain - 1e-9)


@pytest.mark.parametrize(
    "layout,target", [("two", "direct"), ("stacked", "direct"), ("two", "residual")]
)
def test_each_candidate_form_recovers_the_signal(layout: str, target: str) -> None:
    frame = feature_frame(days=120)
    train, test = frame[:760], frame[760:]
    bundle = _bundle(train, layout=layout, target=target)
    dist = bundle.predict(test, "ft")
    margin = (test["target_home_pts"] - test["target_away_pts"]).to_numpy()
    # The true margin spread here is about 16, and predicting zero would miss by far more.
    assert np.corrcoef(dist.mu_margin, margin)[0, 1] > 0.5
    assert np.mean(np.abs(dist.mu_margin - margin)) < np.mean(np.abs(margin))


def test_predicted_spreads_are_roughly_right_and_stay_within_the_bounds() -> None:
    frame = feature_frame(days=120)
    train, test = frame[:760], frame[760:]
    bundle = _bundle(train)
    dist = bundle.predict(test, "ft")
    # Noise is 11 per team with no correlation, plus what the means miss.
    assert 13 < float(np.median(dist.sd_margin)) < 22
    assert 13 < float(np.median(dist.sd_total)) < 22
    for name, values in (("margin", dist.sd_margin), ("total", dist.sd_total)):
        low, high = bundle.ft.sd_bounds[name]
        assert values.min() >= low - 1e-9
        assert values.max() <= high + 1e-9
    assert np.all(np.abs(dist.rho) <= 1.0)


def test_spreads_are_well_scaled_out_of_sample() -> None:
    frame = feature_frame(days=150)
    train, test = frame[:900], frame[900:]
    dist = _bundle(train).predict(test, "ft")
    margin = (test["target_home_pts"] - test["target_away_pts"]).to_numpy()
    ratio = np.mean(((margin - dist.mu_margin) / dist.sd_margin) ** 2)
    assert 0.7 < ratio < 1.4  # The log-target bias correction brings this near 1.


def test_first_half_model_predicts_first_half_scores() -> None:
    frame = feature_frame(days=60)
    bundle = _bundle(frame)
    half = bundle.predict(frame, "h1")
    full = bundle.predict(frame, "ft")
    assert np.mean(half.mu_home) == pytest.approx(np.mean(full.mu_home) / 2, rel=0.1)


def test_missing_first_half_targets_are_left_out_of_that_period() -> None:
    frame = feature_frame(days=40).with_columns(
        pl.when(pl.col("game_id").str.ends_with("0"))
        .then(None)
        .otherwise(pl.col("target_home_1h"))
        .alias("target_home_1h")
    )
    assert _bundle(frame).predict(frame, "h1").mu_home.shape == (len(frame),)


def test_recent_seasons_and_covid_seasons_are_weighted_as_docs_05_says() -> None:
    frame = pl.DataFrame(
        {"season": [2026, 2024, 2021], "season_flag": [None, None, "covid"]},
        schema_overrides={"season_flag": pl.String},
    )
    weights = sample_weights(frame)
    assert weights[0] == pytest.approx(1.0)
    assert weights[1] == pytest.approx(0.5)  # Half-life of two seasons.
    assert weights[2] == pytest.approx(0.5 * 0.5**2.5)


def test_registry_round_trip_gives_identical_predictions(tmp_path: Path) -> None:
    frame = feature_frame(days=40)
    bundle = _bundle(frame)
    folder = save_bundle(bundle, tmp_path, "NBA", "20261106-1000-abc1234", frame)
    restored = load_bundle(folder)
    first, second = bundle.predict(frame, "ft"), restored.predict(frame, "ft")
    assert np.array_equal(first.mu_home, second.mu_home)
    assert np.array_equal(first.sd_total, second.sd_total)
    assert restored.content_hash() == bundle.content_hash()
    assert (folder / "training_data_hash.txt").read_text() == training_data_hash(frame)
    assert (folder / "feature_set_version.txt").read_text() == "v1"


def test_saving_twice_to_the_same_version_is_refused(tmp_path: Path) -> None:
    frame = feature_frame(days=40)
    bundle = _bundle(frame)
    save_bundle(bundle, tmp_path, "NBA", "20261106-1000-abc1234", frame)
    with pytest.raises(FileExistsError):
        save_bundle(bundle, tmp_path, "NBA", "20261106-1000-abc1234", frame)


def test_version_name_marks_uncommitted_code() -> None:
    now = datetime(2026, 11, 6, 10, 5, tzinfo=UTC)
    assert model_version(now, "abcdef1234", dirty=False) == "20261106-1005-abcdef1"
    assert model_version(now, "abcdef1234", dirty=True) == "20261106-1005-abcdef1-dirty"


def test_crps_prefers_the_trained_model_to_a_flat_guess() -> None:
    frame = feature_frame(days=150)
    train, test = frame[:900], frame[900:]
    dist = _bundle(train).predict(test, "ft")
    total = (test["target_home_pts"] + test["target_away_pts"]).to_numpy().astype(float)
    flat = np.full(len(total), total.mean())
    model = crps_normal(total, dist.mu_total, dist.sd_total).mean()
    assert model < crps_normal(total, flat, np.full(len(total), 40.0)).mean()
