"""Calibrators: monotonic, the right kind by sample count, and stable through JSON (docs/05 4.3)."""

import json

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from hoopsedge.models.calibration import ISOTONIC_MIN_SAMPLES, MIN_BLOCK, Calibrator


def _biased_samples(count: int, seed: int = 3) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    raw = rng.uniform(0.2, 0.8, count)
    # The model is overconfident: the truth sits halfway back toward 50%.
    outcome = rng.random(count) < 0.5 + (raw - 0.5) * 0.5
    return raw, outcome.astype(float)


def test_kind_depends_on_the_sample_count() -> None:
    small = Calibrator.fit(*_biased_samples(ISOTONIC_MIN_SAMPLES - 1))
    large = Calibrator.fit(*_biased_samples(ISOTONIC_MIN_SAMPLES))
    assert small.kind == "platt"
    assert large.kind == "isotonic"


@pytest.mark.parametrize("count", [200, 2000])
def test_output_never_decreases_as_input_rises(count: int) -> None:
    calibrator = Calibrator.fit(*_biased_samples(count))
    grid = np.linspace(0.0, 1.0, 501)
    assert np.all(np.diff(calibrator.apply(grid)) >= -1e-12)


@pytest.mark.parametrize("count", [500, 5000])
def test_it_corrects_overconfidence(count: int) -> None:
    calibrator = Calibrator.fit(*_biased_samples(count))
    assert calibrator.apply(np.array([0.75]))[0] == pytest.approx(0.625, abs=0.05)


@pytest.mark.parametrize("count", [200, 2000])
def test_json_round_trip_changes_nothing(count: int) -> None:
    calibrator = Calibrator.fit(*_biased_samples(count))
    restored = Calibrator.from_json(json.loads(json.dumps(calibrator.to_json())))
    grid = np.linspace(0.01, 0.99, 99)
    assert np.array_equal(calibrator.apply(grid), restored.apply(grid))


def test_output_stays_away_from_certainty() -> None:
    raw = np.array([0.1] * 600 + [0.9] * 600)
    outcome = np.array([0.0] * 600 + [1.0] * 600)
    applied = Calibrator.fit(raw, outcome).apply(np.array([0.0, 1.0]))
    assert 0.0 < applied[0] < applied[1] < 1.0


def test_a_few_lucky_rows_at_the_edge_cannot_create_near_certainty() -> None:
    # Real case: at market lines, three rows at the top of the range won, and a plain isotonic fit
    # then called anything up there about 97%. A leg like that would top every ticket.
    rng = np.random.default_rng(5)
    raw = np.concatenate([rng.uniform(0.45, 0.55, 2000), np.array([0.8, 0.82, 0.84])])
    outcome = np.concatenate([(rng.random(2000) < 0.5).astype(float), np.ones(3)])
    applied = Calibrator.fit(raw, outcome).apply(np.array([0.2, 0.84, 0.99]))
    assert applied[1] < 0.7
    assert applied[2] < 0.7
    assert applied[0] > 0.3


def test_every_isotonic_block_rests_on_enough_rows() -> None:
    calibrator = Calibrator.fit(*_biased_samples(3000))
    assert calibrator.kind == "isotonic"
    # Each knot stands for one block, so there can't be more knots than the minimum allows.
    assert len(calibrator.x) <= 3000 // MIN_BLOCK
    assert list(calibrator.x) == sorted(calibrator.x)


def test_too_few_samples_is_an_error_not_a_silent_identity() -> None:
    with pytest.raises(ValueError, match="samples"):
        Calibrator.fit(np.array([0.5, 0.6]), np.array([1.0, 0.0]))


def test_one_outcome_only_is_an_error() -> None:
    with pytest.raises(ValueError, match="both outcomes"):
        Calibrator.fit(np.linspace(0.2, 0.8, 300), np.ones(300))


@settings(max_examples=25, deadline=None)
@given(seed=st.integers(0, 10_000), count=st.integers(100, 1500))
def test_monotonic_on_any_sample(seed: int, count: int) -> None:
    calibrator = Calibrator.fit(*_biased_samples(count, seed))
    assert np.all(np.diff(calibrator.apply(np.linspace(0, 1, 101))) >= -1e-12)
