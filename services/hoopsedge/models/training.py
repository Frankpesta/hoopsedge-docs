"""The mean and variance model families for both periods (docs/05 sections 4.1 and 4.2).

Variance models fit log squared residuals of out-of-fold means. Since E[log e^2] sits below
log E[e^2], each carries a scale, measured out of fold, that restores the true size.
"""

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import lightgbm as lgb
import numpy as np
import polars as pl

from hoopsedge.features.registry import FEATURES, UNTRAINED_FEATURES
from hoopsedge.models.baselines import TARGETS, rate_means, targets_of
from hoopsedge.models.distribution import ScoreDistribution

# D15 keeps injuries out of V1. Travel has no data yet (D56), so it has nothing to teach.
TRAIN_FEATURES: tuple[str, ...] = tuple(
    name for name in FEATURES if name not in UNTRAINED_FEATURES and not name.startswith("travel_km")
)
PERIODS = ("ft", "h1")
SEASON_HALF_LIFE = 2.0
COVID_WEIGHT = 0.5
OOF_FOLDS = 5
LOG_FLOOR = 1.0  # Keeps log(e^2) finite when a prediction is exact.
SD_FLOOR = 0.6  # Predicted spreads stay within these multiples of the training spread.
SD_CAP = 1.6
STACKED_EXTRA = "is_home"
SPREAD_NAMES = ("margin", "total", "home", "away")

DEFAULT_PARAMS: dict[str, Any] = {
    "seed": 20261106,
    "layout": "two",  # "two" models, or one "stacked" on the team's perspective.
    "target": "direct",  # Points, or the "residual" from the ratings baseline.
    "objective": "regression",
    "mean": {
        "n_estimators": 300,
        "learning_rate": 0.03,
        "num_leaves": 15,
        "min_data_in_leaf": 40,
        "feature_fraction": 0.7,
        "lambda_l2": 5.0,
    },
    "var": {
        "n_estimators": 80,
        "learning_rate": 0.03,
        "num_leaves": 7,
        "min_data_in_leaf": 100,
        "feature_fraction": 0.5,
        "lambda_l2": 10.0,
    },
}


def family_settings(params: dict[str, Any], period: str, kind: str) -> dict[str, Any]:
    """A family's own tuned settings, such as `h1_var`, or the shared ones for its kind."""
    settings: dict[str, Any] = params.get(f"{period}_{kind}", params[kind])
    return settings


def sample_weights(frame: pl.DataFrame) -> np.ndarray:
    """Recent seasons count more, with a half-life of two seasons, and COVID seasons count half."""
    seasons = frame["season"].to_numpy().astype(float)
    weights = 0.5 ** ((seasons.max() - seasons) / SEASON_HALF_LIFE)
    flagged = frame["season_flag"].is_not_null().to_numpy()
    weighted: np.ndarray = np.where(flagged, weights * COVID_WEIGHT, weights)
    return weighted


def training_data_hash(frame: pl.DataFrame) -> str:
    """Identifies the rows and results a model saw. Order doesn't matter."""
    rows = frame.select(["game_id", *TARGETS["ft"], *TARGETS["h1"]]).sort("game_id")
    digest = hashlib.sha256()
    for row in rows.iter_rows():
        digest.update(repr(row).encode("utf-8"))
    return digest.hexdigest()


def _matrix(frame: pl.DataFrame, names: Sequence[str]) -> np.ndarray:
    return frame.select(pl.col(name).cast(pl.Float64) for name in names).to_numpy()


def _swap(name: str) -> str:
    if name.endswith("_h"):
        return f"{name[:-2]}_a"
    if name.endswith("_a"):
        return f"{name[:-2]}_h"
    return name


def _stacked_matrix(frame: pl.DataFrame, side: str) -> np.ndarray:
    """Features seen from one team: `_h` columns always hold that team's own values."""
    names = list(TRAIN_FEATURES) if side == "home" else [_swap(n) for n in TRAIN_FEATURES]
    matrix = _matrix(frame, names)
    if side == "away":
        matrix[:, TRAIN_FEATURES.index("elo_diff")] *= -1.0
    flag = 1.0 - frame["neutral"].to_numpy() if side == "home" else np.zeros(len(frame))
    return np.column_stack([matrix, flag])


def _constraints(names: Sequence[str], own: str) -> list[int]:
    """Better own offence, or worse opposing defence, can't lower a team's expected points."""
    opponent = "a" if own == "h" else "h"
    rising = {f"adj_o_{own}", f"adj_d_{opponent}"}
    return [1 if name in rising else 0 for name in names]


def _train(
    matrix: np.ndarray,
    target: np.ndarray,
    weights: np.ndarray,
    settings: dict[str, Any],
    names: Sequence[str],
    seed: int,
    objective: str = "regression",
    monotone: list[int] | None = None,
) -> lgb.Booster:
    params = dict(settings)
    rounds = int(params.pop("n_estimators"))
    params.update(
        objective=objective,
        seed=seed,
        deterministic=True,
        force_row_wise=True,
        num_threads=4,
        verbosity=-1,
        linear_tree=False,
    )
    if monotone is not None:
        params["monotone_constraints"] = monotone
        params["monotone_constraints_method"] = "basic"
    dataset = lgb.Dataset(
        matrix, label=target, weight=weights, feature_name=list(names), free_raw_data=True
    )
    return lgb.train(params, dataset, num_boost_round=rounds)


def _folds(frame: pl.DataFrame, count: int = OOF_FOLDS) -> np.ndarray:
    """Whole slates go to a fold, so games on one night never inform each other."""
    days = frame["slate_date_et"].to_numpy()
    unique = np.unique(days)
    return np.searchsorted(unique, days) % count


@dataclass
class PeriodModel:
    period: str
    layout: str
    target: str
    mean: dict[str, lgb.Booster]
    var: dict[str, lgb.Booster]
    scale: dict[str, float]
    sd_bounds: dict[str, tuple[float, float]]

    def predict_means(self, frame: pl.DataFrame) -> tuple[np.ndarray, np.ndarray]:
        return _predict_means(self.mean, frame, self.period, self.layout, self.target)

    def predict_spreads(self, frame: pl.DataFrame) -> dict[str, np.ndarray]:
        inputs = _matrix(frame, TRAIN_FEATURES)
        spreads: dict[str, np.ndarray] = {}
        for name in SPREAD_NAMES:
            raw = np.sqrt(np.exp(self.var[name].predict(inputs)) * self.scale[name])
            spreads[name] = np.clip(raw, *self.sd_bounds[name])
        return spreads

    def predict(self, frame: pl.DataFrame) -> ScoreDistribution:
        mu_home, mu_away = self.predict_means(frame)
        spreads = self.predict_spreads(frame)
        return ScoreDistribution(
            mu_home, mu_away, spreads["margin"], spreads["total"], spreads["home"], spreads["away"]
        )


def _predict_means(
    boosters: dict[str, lgb.Booster],
    frame: pl.DataFrame,
    period: str,
    layout: str,
    target: str,
) -> tuple[np.ndarray, np.ndarray]:
    if layout == "stacked":
        home = np.asarray(boosters["team"].predict(_stacked_matrix(frame, "home")))
        away = np.asarray(boosters["team"].predict(_stacked_matrix(frame, "away")))
    else:
        inputs = _matrix(frame, TRAIN_FEATURES)
        home = np.asarray(boosters["home"].predict(inputs))
        away = np.asarray(boosters["away"].predict(inputs))
    if target == "residual":
        base_home, base_away = rate_means(frame, period)
        return base_home + home, base_away + away
    return home, away


def _fit_means(frame: pl.DataFrame, period: str, params: dict[str, Any]) -> dict[str, lgb.Booster]:
    home, away = targets_of(frame, period)
    if params["target"] == "residual":
        base_home, base_away = rate_means(frame, period)
        home, away = home - base_home, away - base_away
    weights = sample_weights(frame)
    settings = family_settings(params, period, "mean")
    seed, objective = int(params["seed"]), params["objective"]
    if params["layout"] == "stacked":
        names = [*TRAIN_FEATURES, STACKED_EXTRA]
        matrix = np.vstack([_stacked_matrix(frame, "home"), _stacked_matrix(frame, "away")])
        target = np.concatenate([home, away])
        doubled = np.concatenate([weights, weights])
        monotone = _constraints(names, "h")
        return {"team": _train(matrix, target, doubled, settings, names, seed, objective, monotone)}
    inputs = _matrix(frame, TRAIN_FEATURES)
    return {
        "home": _train(
            inputs, home, weights, settings, TRAIN_FEATURES, seed, objective,
            _constraints(TRAIN_FEATURES, "h"),
        ),
        "away": _train(
            inputs, away, weights, settings, TRAIN_FEATURES, seed, objective,
            _constraints(TRAIN_FEATURES, "a"),
        ),
    }  # fmt: skip


def _residual_targets(
    home: np.ndarray, away: np.ndarray, mean_home: np.ndarray, mean_away: np.ndarray
) -> dict[str, np.ndarray]:
    error_home, error_away = home - mean_home, away - mean_away
    return {
        "margin": error_home - error_away,
        "total": error_home + error_away,
        "home": error_home,
        "away": error_away,
    }


def usable_rows(frame: pl.DataFrame, period: str) -> pl.DataFrame:
    return frame.filter(
        pl.col(TARGETS[period][0]).is_not_null() & pl.col(TARGETS[period][1]).is_not_null()
    )


def out_of_fold_means(
    usable: pl.DataFrame, period: str, params: dict[str, Any]
) -> tuple[np.ndarray, np.ndarray]:
    """Means for each row from models that didn't see its slate."""
    folds = _folds(usable)
    oof_home, oof_away = np.zeros(len(usable)), np.zeros(len(usable))
    for fold in range(OOF_FOLDS):
        held = folds == fold
        if not held.any():
            continue
        boosters = _fit_means(usable.filter(pl.Series(~held)), period, params)
        oof_home[held], oof_away[held] = _predict_means(
            boosters, usable.filter(pl.Series(held)), period, params["layout"], params["target"]
        )
    return oof_home, oof_away


def fit_variance(
    usable: pl.DataFrame,
    period: str,
    params: dict[str, Any],
    oof_home: np.ndarray,
    oof_away: np.ndarray,
) -> tuple[dict[str, lgb.Booster], dict[str, float], dict[str, tuple[float, float]]]:
    """Spread models from honest residuals, each scaled back to the true size out of fold."""
    home, away = targets_of(usable, period)
    residuals = _residual_targets(home, away, oof_home, oof_away)
    folds = _folds(usable)
    inputs = _matrix(usable, TRAIN_FEATURES)
    weights = sample_weights(usable)
    settings = family_settings(params, period, "var")
    seed = int(params["seed"])
    models: dict[str, lgb.Booster] = {}
    scale: dict[str, float] = {}
    bounds: dict[str, tuple[float, float]] = {}
    for name in SPREAD_NAMES:
        squared = residuals[name] ** 2
        log_target = np.log(squared + LOG_FLOOR)
        predicted = np.zeros(len(usable))
        for fold in range(OOF_FOLDS):
            held = folds == fold
            if not held.any():
                continue
            booster = _train(
                inputs[~held], log_target[~held], weights[~held], settings, TRAIN_FEATURES, seed
            )
            predicted[held] = booster.predict(inputs[held])
        scale[name] = float(np.average(squared / np.exp(predicted), weights=weights))
        models[name] = _train(inputs, log_target, weights, settings, TRAIN_FEATURES, seed)
        spread = float(np.sqrt(np.average(squared, weights=weights)))
        bounds[name] = (spread * SD_FLOOR, spread * SD_CAP)
    return models, scale, bounds


def fit_period(frame: pl.DataFrame, period: str, params: dict[str, Any]) -> PeriodModel:
    usable = usable_rows(frame, period)
    oof_home, oof_away = out_of_fold_means(usable, period, params)
    models, scale, bounds = fit_variance(usable, period, params, oof_home, oof_away)
    return PeriodModel(
        period,
        params["layout"],
        params["target"],
        _fit_means(usable, period, params),
        models,
        scale,
        bounds,
    )


@dataclass
class ModelBundle:
    ft: PeriodModel
    h1: PeriodModel
    params: dict[str, Any] = field(default_factory=dict)

    def period(self, period: str) -> PeriodModel:
        return self.ft if period == "ft" else self.h1

    def predict(self, frame: pl.DataFrame, period: str) -> ScoreDistribution:
        return self.period(period).predict(frame)

    def content_hash(self) -> str:
        """SHA-256 of every booster, scale and bound. Same data and seed give the same hash."""
        digest = hashlib.sha256()
        for model in (self.ft, self.h1):
            for group in (model.mean, model.var):
                for name in sorted(group):
                    digest.update(name.encode("utf-8"))
                    digest.update(_booster_text(group[name]).encode("utf-8"))
            digest.update(repr(sorted(model.scale.items())).encode("utf-8"))
            digest.update(repr(sorted(model.sd_bounds.items())).encode("utf-8"))
        return digest.hexdigest()


def _booster_text(booster: lgb.Booster) -> str:
    """The trees alone. The parameters block records thread counts and such, which can vary."""
    text: str = booster.model_to_string()
    return text.split("parameters:", 1)[0]


def fit_bundle(frame: pl.DataFrame, params: dict[str, Any]) -> ModelBundle:
    return ModelBundle(fit_period(frame, "ft", params), fit_period(frame, "h1", params), params)
