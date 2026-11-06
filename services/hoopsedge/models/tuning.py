"""Small-budget hyperparameter search, run once locally (docs/05 section 4.2).

Chosen values are committed to `models/params/{league}.yaml`. Nightly learning only refits with
them. Optuna is a dev dependency, so it stays out of the Lambda image.
"""

from collections.abc import Callable
from typing import Any, Literal

import numpy as np
import optuna
import polars as pl

from hoopsedge.models.baselines import targets_of
from hoopsedge.models.distribution import log_likelihood_normal
from hoopsedge.models.training import (
    DEFAULT_PARAMS,
    PeriodModel,
    _fit_means,
    _predict_means,
    fit_variance,
    out_of_fold_means,
    usable_rows,
)

MAX_TRIALS = 40
FORMS = (
    {"layout": "two", "target": "direct"},
    {"layout": "stacked", "target": "direct"},
    {"layout": "two", "target": "residual"},
    {"layout": "stacked", "target": "residual"},
)
OBJECTIVES = ("regression", "huber")
Log = Callable[[str], None]


def _mean_trial(trial: optuna.Trial) -> dict[str, Any]:
    return {
        "n_estimators": trial.suggest_int("n_estimators", 100, 600, step=50),
        "learning_rate": trial.suggest_float("learning_rate", 0.01, 0.08, log=True),
        "num_leaves": trial.suggest_int("num_leaves", 4, 31),
        "min_data_in_leaf": trial.suggest_int("min_data_in_leaf", 20, 200),
        "feature_fraction": trial.suggest_float("feature_fraction", 0.3, 1.0),
        "lambda_l2": trial.suggest_float("lambda_l2", 0.1, 50.0, log=True),
    }


def _var_trial(trial: optuna.Trial) -> dict[str, Any]:
    return {
        "n_estimators": trial.suggest_int("n_estimators", 20, 200, step=20),
        "learning_rate": trial.suggest_float("learning_rate", 0.01, 0.08, log=True),
        "num_leaves": trial.suggest_int("num_leaves", 3, 15),
        "min_data_in_leaf": trial.suggest_int("min_data_in_leaf", 50, 400),
        "feature_fraction": trial.suggest_float("feature_fraction", 0.2, 1.0),
        "lambda_l2": trial.suggest_float("lambda_l2", 1.0, 100.0, log=True),
    }


def mean_score(
    train: pl.DataFrame, valid: pl.DataFrame, period: str, params: dict[str, Any]
) -> float:
    """Validation RMSE over margin and total, the two quantities markets depend on most."""
    boosters = _fit_means(train, period, params)
    mu_home, mu_away = _predict_means(boosters, valid, period, params["layout"], params["target"])
    home, away = targets_of(valid, period)
    margin = (home - away) - (mu_home - mu_away)
    total = (home + away) - (mu_home + mu_away)
    return float(np.sqrt(np.mean(np.concatenate([margin**2, total**2]))))


def spread_score(
    train: pl.DataFrame,
    valid: pl.DataFrame,
    period: str,
    params: dict[str, Any],
    oof: tuple[np.ndarray, np.ndarray],
    valid_means: tuple[np.ndarray, np.ndarray],
) -> float:
    """Validation Gaussian log-likelihood of margin and total, given fixed means."""
    models, scale, bounds = fit_variance(train, period, params, *oof)
    model = PeriodModel(period, params["layout"], params["target"], {}, models, scale, bounds)
    spreads = model.predict_spreads(valid)
    home, away = targets_of(valid, period)
    mu_home, mu_away = valid_means
    return float(
        np.mean(
            log_likelihood_normal(home - away, mu_home - mu_away, spreads["margin"])
            + log_likelihood_normal(home + away, mu_home + mu_away, spreads["total"])
        )
    )


def _study(direction: Literal["minimize", "maximize"], seed: int) -> optuna.Study:
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    return optuna.create_study(direction=direction, sampler=optuna.samplers.TPESampler(seed=seed))


def _mean_objective(
    params: dict[str, Any], train: pl.DataFrame, valid: pl.DataFrame, period: str
) -> Callable[[optuna.Trial], float]:
    def objective(trial: optuna.Trial) -> float:
        candidate = {**params, f"{period}_mean": _mean_trial(trial)}
        return mean_score(train, valid, period, candidate)

    return objective


def _var_objective(
    params: dict[str, Any],
    train: pl.DataFrame,
    valid: pl.DataFrame,
    period: str,
    oof: tuple[np.ndarray, np.ndarray],
    means: tuple[np.ndarray, np.ndarray],
) -> Callable[[optuna.Trial], float]:
    def objective(trial: optuna.Trial) -> float:
        candidate = {**params, f"{period}_var": _var_trial(trial)}
        return spread_score(train, valid, period, candidate, oof, means)

    return objective


def tune_league(
    train: pl.DataFrame,
    valid: pl.DataFrame,
    trials: int = MAX_TRIALS,
    log: Log = print,
) -> dict[str, Any]:
    """Pick the form on default settings, then each family's settings. One fold, one seed."""
    if trials > MAX_TRIALS:
        raise ValueError(f"The budget is {MAX_TRIALS} trials per family (docs/05)")
    params: dict[str, Any] = dict(DEFAULT_PARAMS)
    seed = int(params["seed"])

    forms: list[dict[str, Any]] = []
    for form in FORMS:
        for objective in OBJECTIVES:
            trial_params = {**params, **form, "objective": objective}
            score = float(
                np.mean(
                    [
                        mean_score(
                            usable_rows(train, period), usable_rows(valid, period), period,
                            trial_params,
                        )
                        for period in ("ft", "h1")
                    ]
                )
            )  # fmt: skip
            forms.append({**form, "objective": objective, "rmse": round(score, 4)})
            log(f"form {form} {objective}: RMSE {score:.3f}")
    best_form = min(forms, key=lambda row: row["rmse"])
    params.update(
        layout=best_form["layout"], target=best_form["target"], objective=best_form["objective"]
    )
    tuning_record: dict[str, Any] = {"forms": forms, "families": {}}

    for period in ("ft", "h1"):
        train_rows = usable_rows(train, period)
        valid_rows = usable_rows(valid, period)

        study = _study("minimize", seed)
        study.optimize(_mean_objective(params, train_rows, valid_rows, period), n_trials=trials)
        params[f"{period}_mean"] = dict(study.best_params)
        tuning_record["families"][f"{period}_mean"] = round(float(study.best_value), 4)
        log(f"{period}_mean: RMSE {study.best_value:.3f} {study.best_params}")

        oof = out_of_fold_means(train_rows, period, params)
        means = _predict_means(
            _fit_means(train_rows, period, params),
            valid_rows,
            period,
            params["layout"],
            params["target"],
        )

        study = _study("maximize", seed)
        study.optimize(
            _var_objective(params, train_rows, valid_rows, period, oof, means), n_trials=trials
        )
        params[f"{period}_var"] = dict(study.best_params)
        tuning_record["families"][f"{period}_var"] = round(float(study.best_value), 4)
        log(f"{period}_var: log-likelihood {study.best_value:.4f} {study.best_params}")

    return {"params": params, "tuning": tuning_record}
