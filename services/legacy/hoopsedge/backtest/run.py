"""Phase 4 jobs, in the order they run.

    docker compose run --rm dev python -m hoopsedge.backtest.run lines
    docker compose run --rm dev python -m hoopsedge.backtest.run ratings-grid
    docker compose run --rm dev python -m hoopsedge.backtest.run tune --league NBA
    docker compose run --rm dev python -m hoopsedge.backtest.run backtest --league NBA
    docker compose run --rm dev python -m hoopsedge.backtest.run evidence --league NBA

Set HOOPSEDGE_GIT_SHA to the commit being run, or the model version is marked `-dirty`.
"""

import argparse
import json
import os
import sys
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
import yaml

from hoopsedge.backtest.data import (
    load_frame,
    load_games,
    load_lines,
    load_predictions,
    predictions_path,
)
from hoopsedge.backtest.distribution_choice import compare_forms
from hoopsedge.backtest.evidence import (
    REAL_FAMILIES,
    apply_overrides,
    both_sides,
    calibrate_by_window,
    early_season_table,
    gates,
    market_line_table,
    market_table,
    reliability,
    score_table,
    selection_table,
)
from hoopsedge.backtest.lines import CLOSE, build_lines
from hoopsedge.backtest.markets import (
    FAMILIES,
    MODEL,
    REAL,
    SYNTHETIC,
    score_markets,
    with_calibration,
)
from hoopsedge.backtest.metrics import sweep_thresholds
from hoopsedge.backtest.plan import PLANS, Plan
from hoopsedge.backtest.ratings_grid import Setting, priced_d1_mask, replay, summarize
from hoopsedge.backtest.report import write_report
from hoopsedge.backtest.walkforward import known_before, margin_errors, predict_window
from hoopsedge.ingest.espn_coverage import EASTERN
from hoopsedge.models.calibration import Calibrator
from hoopsedge.models.registry import model_version, save_bundle
from hoopsedge.models.training import (
    TRAIN_FEATURES,
    ModelBundle,
    _matrix,
    _stacked_matrix,
    fit_bundle,
)
from hoopsedge.ratings.history import History

DEFAULT_DATA_DIR = "/workspace/data"
DEFAULT_REPORT_DIR = "/workspace/docs/reports"
PARAMS_DIR = Path(__file__).parent.parent / "models" / "params"
SHAP_SAMPLE = 1000
TOP_FEATURES = 15
GRID_SETTINGS = {
    "NBA": [Setting(r, k, 100.0) for r in (0.2, 0.35, 0.5) for k in (4.0, 8.0, 16.0)],
    "NCAA": [Setting(r, k, 1000.0) for r in (0.1, 0.25, 0.45) for k in (1.0, 2.0, 3.0, 6.0)],
}
# Only seasons before the tested ones set the values. NCAA's later seasons are shown to compare.
GRID_SEASONS = {"NBA": [2020, 2022, 2023], "NCAA": [2025, 2026, 2027]}


def load_params(league: str) -> dict[str, Any]:
    path = PARAMS_DIR / f"{league}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"No tuned settings at {path}: run `tune` first")
    params: dict[str, Any] = yaml.safe_load(path.read_text("utf-8"))["params"]
    return params


def tune(data_dir: Path, league: str, trials: int) -> None:
    from hoopsedge.models.tuning import tune_league  # Optuna is a dev dependency.

    plan = PLANS[league]
    frame = load_frame(data_dir, league)
    train = known_before(frame, league, plan.validation.start)
    valid = frame.filter(
        (pl.col("slate_date_et") >= plan.validation.start)
        & (pl.col("slate_date_et") < plan.validation.end)
    )
    print(f"{league}: tuning on {len(train)} rows, validating on {len(valid)}", flush=True)
    result = tune_league(train, valid, trials)
    PARAMS_DIR.mkdir(parents=True, exist_ok=True)
    (PARAMS_DIR / f"{league}.yaml").write_text(yaml.safe_dump(result, sort_keys=True), "utf-8")


def backtest(data_dir: Path, league: str) -> None:
    plan = PLANS[league]
    frame = load_frame(data_dir, league)
    params = load_params(league)
    latest: date = frame["slate_date_et"].max()  # type: ignore[assignment]
    windows = [*plan.validation_windows(), *[w for _, w in plan.test_windows(latest)]]
    parts: list[pl.DataFrame] = []
    started = time.monotonic()
    for number, window in enumerate(windows, 1):
        part = predict_window(frame, window, params, league)
        if len(part):
            parts.append(part)
            errors = {k: round(v, 2) for k, v in margin_errors(part).items()}
            print(
                f"{league} {number}/{len(windows)} {window.start}: {part['game_id'].n_unique()} "
                f"games, margin MAE {errors}, {time.monotonic() - started:.0f} s",
                flush=True,
            )
    path = predictions_path(data_dir, league)
    path.parent.mkdir(parents=True, exist_ok=True)
    pl.concat(parts).write_parquet(path)


def validation_thresholds(scored: pl.DataFrame, plan: Plan) -> dict[str, dict[str, float] | None]:
    """Calibrate on the first half of validation, and sweep on the second half."""
    rows = scored.filter(
        (pl.col("model") == MODEL)
        & (pl.col("slate_date_et") >= plan.validation.start)
        & (pl.col("slate_date_et") < plan.validation.end)
    )
    days = rows["slate_date_et"].unique().sort()
    middle: date = days[len(days) // 2]
    calibrated = with_calibration(rows, middle, plan.validation.end)
    chosen: dict[str, dict[str, float] | None] = {}
    for family in FAMILIES:
        source = REAL if family in REAL_FAMILIES else SYNTHETIC
        sides = both_sides(calibrated.filter(pl.col("family") == family), source)
        # D10: only the single best outcome per game is ever eligible.
        best = sides.sort("p_calibrated", "game_id", descending=[True, False]).unique(
            "game_id", keep="first", maintain_order=True
        )
        found = sweep_thresholds(best, plan.leg_floor(family))
        chosen[family] = (
            None
            if found is None
            else {
                "min_prob": found.min_prob,
                "min_edge": found.min_edge,
                "log_likelihood": round(found.log_likelihood, 5),
                "legs": found.legs,
                "legs_per_slate": round(found.legs_per_slate, 2),
            }
        )
    return chosen


def _label_rows(frame: pl.DataFrame, plan: Plan) -> pl.DataFrame:
    column = pl.lit(None, dtype=pl.String)
    for test in plan.tests:
        column = (
            pl.when(pl.col("slate_date_et").is_between(test.first, test.last))
            .then(pl.lit(test.label))
            .otherwise(column)
        )
    return frame.with_columns(column.alias("evidence_period"))


def _importance(bundle: ModelBundle, frame: pl.DataFrame) -> list[dict[str, Any]]:
    """Gain, and SHAP from LightGBM's own pred_contrib, which keeps the image small (BUILD-PLAN)."""
    model = bundle.ft
    if model.layout == "stacked":
        booster = model.mean["team"]
        sample = frame.sample(min(SHAP_SAMPLE, len(frame)), seed=1)
        matrix = _stacked_matrix(sample, "home")
    else:
        booster = model.mean["home"]
        matrix = _matrix(frame.sample(min(SHAP_SAMPLE, len(frame)), seed=1), TRAIN_FEATURES)
    names = booster.feature_name()
    gain = booster.feature_importance("gain").astype(float)
    shap = np.abs(booster.predict(matrix, pred_contrib=True))[:, :-1].mean(axis=0)
    order = np.argsort(-shap)[:TOP_FEATURES]
    return [
        {
            "feature": names[i],
            "gain_share": round(float(gain[i] / gain.sum()), 4),
            "shap": round(float(shap[i]), 3),
        }
        for i in order
    ]


def _coverage(frame: pl.DataFrame, lines: pl.DataFrame, plan: Plan) -> list[dict[str, Any]]:
    closing = lines.filter(pl.col("kind") == CLOSE)
    rows: list[dict[str, Any]] = []
    for test in plan.tests:
        games = frame.filter(pl.col("slate_date_et").is_between(test.first, test.last))
        if games.is_empty():
            continue
        rows.append(
            {
                "label": test.label,
                "games": len(games),
                "closing_lines": games.join(closing, on="game_id", how="semi").height,
            }
        )
    return rows


def _load_overrides(league: str) -> dict[str, dict[str, Any]]:
    """Hand-kept and logged in DECISIONS, so regenerating evidence can't erase one."""
    path = PARAMS_DIR / "market_overrides.yaml"
    overrides: dict[str, dict[str, dict[str, Any]]] = yaml.safe_load(path.read_text("utf-8"))
    return overrides.get(league) or {}


def _save_gates(league: str, decisions: dict[str, Any], thresholds: dict[str, Any]) -> None:
    for name, payload in (
        (
            "market_gates",
            {
                family: {
                    "enabled": gate["enabled"],
                    "passed": gate["passed"],
                    "override": gate["override"],
                    "judged_on": gate["judged_on"],
                    "edge": gate["edge"],
                    "reason": gate["reason"],
                }
                for family, gate in decisions.items()
            },
        ),
        ("thresholds", thresholds),
    ):
        path = PARAMS_DIR / f"{name}.yaml"
        current = yaml.safe_load(path.read_text("utf-8")) if path.exists() else {}
        current[league] = payload
        path.write_text(yaml.safe_dump(current, sort_keys=True), "utf-8")


def evidence(data_dir: Path, report_dir: Path, league: str) -> Path:
    plan = PLANS[league]
    frame = load_frame(data_dir, league)
    params = load_params(league)
    predictions = load_predictions(data_dir, league)
    lines = load_lines(data_dir, league)
    latest: date = frame["slate_date_et"].max()  # type: ignore[assignment]

    # D27: `current` isn't a closing line. Validation has nothing else, so it may use them there;
    # the evidence itself uses only closing lines.
    validation_ids = predictions.filter(pl.col("slate_date_et") < plan.validation.end)["game_id"]
    usable_lines = lines.filter(
        (pl.col("kind") == CLOSE) | pl.col("game_id").is_in(validation_ids.unique().to_list())
    )
    scored = score_markets(predictions, usable_lines)
    windows = plan.test_windows(latest)
    starts = [window.start for _, window in windows]
    calibrated = calibrate_by_window(scored, starts, windows[-1][1].end)
    tested = predictions.filter(pl.col("slate_date_et") >= plan.validation.end)

    thresholds = validation_thresholds(scored, plan)
    markets = market_table(calibrated)
    decisions = apply_overrides(gates(markets), _load_overrides(league))
    usable_thresholds = {family: t for family, t in thresholds.items() if t is not None}
    closing = lines.filter(pl.col("kind") == CLOSE)
    priced = set(closing["game_id"].to_list())

    labelled = _label_rows(tested, plan)
    by_label = {
        test.label: score_table(labelled.filter(pl.col("evidence_period") == test.label))
        for test in plan.tests
        if labelled.filter(pl.col("evidence_period") == test.label).height
    }
    train_before = known_before(frame, league, plan.validation.start)
    extra = float(
        train_before.filter(pl.col("target_ot") == 1)
        .select((pl.col("target_home_pts") + pl.col("target_away_pts")).mean())
        .item()
        - train_before.filter(pl.col("target_ot") == 0)
        .select((pl.col("target_home_pts") + pl.col("target_away_pts")).mean())
        .item()
    )
    validation_rows = predictions.filter(
        (pl.col("slate_date_et") >= plan.validation.start)
        & (pl.col("slate_date_et") < plan.validation.end)
    )
    forms = compare_forms(validation_rows, extra)

    # The final artifact uses everything known today, with calibrators from all walk-forward rows.
    today = datetime.now(UTC).astimezone(EASTERN).date()
    final_rows = known_before(frame, league, today + timedelta(days=1))
    bundle = fit_bundle(final_rows, params)
    sha = os.environ.get("HOOPSEDGE_GIT_SHA")
    version = model_version(datetime.now(UTC), sha or "unknown", dirty=sha is None)
    folder = save_bundle(bundle, data_dir / "models", league, version, final_rows)
    (folder / "calibrators").mkdir()
    model_rows = scored.filter((pl.col("model") == MODEL) & pl.col("outcome").is_not_null())
    for family in FAMILIES:
        source = REAL if family in REAL_FAMILIES else SYNTHETIC
        fit = model_rows.filter((pl.col("family") == family) & (pl.col("source") == source))
        calibrator = Calibrator.fit(fit["p"].to_numpy(), fit["outcome"].to_numpy())
        (folder / f"calibrators/{family}.json").write_text(
            json.dumps(calibrator.to_json()), "utf-8"
        )

    notes = [
        "Tuning and thresholds used only games before the first tested game.",
        f"Validation ran from {plan.validation.start} to {plan.validation.end}, refitting every "
        f"{plan.refit_days} days through the tested games.",
        "No injury features are in V1 (D15), so availability news isn't reflected.",
        "Half-time and team totals have no historical lines, so any edge on them is unproven "
        "until live SportyBet logs build up (D18, D27).",
        "Real lines are ESPN book closing lines, not SportyBet prices, whose margin may differ.",
    ]
    if league == "NCAA":
        notes += [
            "History starts at 2023-24 (D66), so only one complete season, 2025-26, is tested. "
            "That is fewer than docs/05's three, and it can't show stability across seasons.",
            "`r` and `k` were chosen on 2024-25 only, before any tested game (D67).",
            "2026-27 so far is a few slates, and real lines stop where the odds download stopped, "
            "so the coverage table shows how few of its games have one.",
            "Handicap and Full-time O/U thresholds use a floor of 1 leg per slate by owner's "
            "decision, because none met the usual 10 (D69). They can't fill a normal ticket.",
        ]
    result: dict[str, Any] = {
        "league": league,
        "version": version,
        "coverage": _coverage(frame, lines, plan),
        "notes": notes,
        "gates": decisions,
        "thresholds": thresholds,
        "scores": score_table(tested),
        "scores_by_label": by_label,
        "markets": markets,
        "market_lines": market_line_table(tested, closing),
        "selection": selection_table(calibrated, usable_thresholds),
        "reliability": reliability(calibrated),
        "early_all": early_season_table(tested, None),
        "early_priced": early_season_table(tested, priced),
        "forms": forms,
        "importance": _importance(bundle, final_rows),
    }
    (folder / "metrics.json").write_text(
        json.dumps({"scores": result["scores"], "markets": markets}, indent=1), "utf-8"
    )
    (folder / "evidence.json").write_text(json.dumps(result, indent=1, sort_keys=True), "utf-8")
    _save_gates(league, decisions, thresholds)
    return write_report(result, report_dir, today.isoformat())


def ratings_grid_report(data_dir: Path, report_dir: Path) -> None:
    """The D60 evidence, on the games that can become tickets: both D1, with a real line."""
    lines = ["# Phase 4: Ratings Settings", "", "Generated by `hoopsedge.backtest.run`."]
    payload: dict[str, Any] = {}
    for league, settings in GRID_SETTINGS.items():
        seasons = GRID_SEASONS[league]
        # Every season in between too: each one's priors come from the season before it.
        history = History.load(data_dir, league, list(range(min(seasons) - 1, max(seasons) + 1)))
        priced = set(load_lines(data_dir, league)["game_id"].to_list())
        lines += [
            "",
            f"## {league}",
            "",
            "| Season | r | k | Games | First 14 days MAE | Slope | After day 45 MAE | Slope "
            "| All MAE | All total MAE |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
        payload[league] = []
        for setting in settings:
            for season in seasons:
                outcome = replay(history, setting, season)
                mask = priced_d1_mask(history, season, priced)
                early = summarize(outcome, 0, 13, mask)
                late = summarize(outcome, 45, 10_000, mask)
                overall = summarize(outcome, 0, 10_000, mask)
                payload[league].append(
                    {"season": season, **vars(setting), "early": early, "late": late,
                     "all": overall}
                )  # fmt: skip
                lines.append(
                    f"| {season} | {setting.r} | {setting.k} | {overall.get('games')} "
                    f"| {early.get('margin_mae')} | {early.get('slope')} "
                    f"| {late.get('margin_mae')} | {late.get('slope')} "
                    f"| {overall.get('margin_mae')} | {overall.get('total_mae')} |"
                )
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "phase4-ratings-grid.md").write_text("\n".join(lines) + "\n", "utf-8")
    (report_dir / "phase4-ratings-grid.json").write_text(
        json.dumps(payload, indent=1, sort_keys=True) + "\n", "utf-8"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("lines")
    commands.add_parser("ratings-grid")
    for name in ("tune", "backtest", "evidence"):
        sub = commands.add_parser(name)
        sub.add_argument("--league", choices=sorted(PLANS), required=True)
        if name == "tune":
            sub.add_argument("--trials", type=int, default=40)
    args = parser.parse_args()
    data_dir = Path(os.environ.get("HOOPSEDGE_DATA_DIR", DEFAULT_DATA_DIR))
    report_dir = Path(os.environ.get("HOOPSEDGE_REPORT_DIR", DEFAULT_REPORT_DIR))
    if args.command == "lines":
        for league in PLANS:
            print(league, build_lines(data_dir, league, load_games(data_dir, league)))
    elif args.command == "ratings-grid":
        ratings_grid_report(data_dir, report_dir)
    elif args.command == "tune":
        tune(data_dir, args.league, args.trials)
    elif args.command == "backtest":
        backtest(data_dir, args.league)
    else:
        print(evidence(data_dir, report_dir, args.league))
    return 0


if __name__ == "__main__":
    sys.exit(main())
