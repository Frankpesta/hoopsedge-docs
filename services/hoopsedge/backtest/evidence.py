"""Evidence tables from walk-forward predictions (docs/05 sections 4.4 and 4.5, D14-D18).

Everything is paired: a candidate and the baseline are compared on the same games and lines, and
intervals resample whole games. Market claims need real lines: where history has none, a family's
probability scores can still be measured, but its edge over the market stays "unproven" (D18).
"""

from datetime import date
from typing import Any

import numpy as np
import polars as pl

from hoopsedge.backtest.markets import (
    BASELINE,
    FAMILIES,
    MODEL,
    REAL,
    SYNTHETIC,
    with_calibration,
)
from hoopsedge.backtest.metrics import (
    Interval,
    bootstrap_difference,
    brier,
    flat_stake_return,
    gate_decision,
    log_loss,
)
from hoopsedge.markets.probability import no_vig_probability
from hoopsedge.models.distribution import crps_normal, log_likelihood_normal

MARGIN_TOTAL = {"margin": "sd_margin", "total": "sd_total"}
REAL_FAMILIES = ("handicap", "total_ft")
PAIR_KEYS = ["game_id", "family", "source", "selection", "line"]
EARLY_BUCKETS = (("1-5 games", 0, 5), ("6-10 games", 6, 10), ("11+ games", 11, 100_000))
BUCKET_EDGES = np.linspace(0.0, 1.0, 11)
FIRST_DAYS = 14


def _interval(value: Interval) -> dict[str, float]:
    return {
        "mean": round(value.mean, 5),
        "low": round(value.low, 5),
        "high": round(value.high, 5),
        "games": value.count,
    }


def _score_arrays(group: pl.DataFrame, target: str) -> dict[str, np.ndarray]:
    home, away = group["actual_home"].to_numpy(), group["actual_away"].to_numpy()
    mu_home, mu_away = group["mu_home"].to_numpy(), group["mu_away"].to_numpy()
    sign = -1.0 if target == "margin" else 1.0
    actual, mean = home + sign * away, mu_home + sign * mu_away
    sd = group[MARGIN_TOTAL[target]].to_numpy()
    return {
        "ae": np.abs(actual - mean),
        "se": (actual - mean) ** 2,
        "crps": crps_normal(actual, mean, sd),
        "ll": log_likelihood_normal(actual, mean, sd),
    }


def score_table(predictions: pl.DataFrame) -> list[dict[str, Any]]:
    """MAE, RMSE, CRPS and log-likelihood of margin and total, against the ratings baseline."""
    rows: list[dict[str, Any]] = []
    usable = predictions.filter(
        pl.col("actual_home").is_not_null() & pl.col("actual_away").is_not_null()
    )
    for period in ("ft", "h1"):
        in_period = usable.filter(pl.col("period") == period)
        base = in_period.filter(pl.col("model") == BASELINE)
        for target in MARGIN_TOTAL:
            for (model,), group in in_period.group_by(["model"], maintain_order=True):
                aligned_base = base.join(group.select("game_id"), on="game_id", how="semi").sort(
                    "game_id"
                )
                aligned = group.join(aligned_base.select("game_id"), on="game_id", how="semi").sort(
                    "game_id"
                )
                if aligned.is_empty():
                    continue
                arrays = _score_arrays(aligned, target)
                base_arrays = _score_arrays(aligned_base, target)
                games = aligned["game_id"].to_numpy()
                rows.append(
                    {
                        "period": period,
                        "target": target,
                        "model": model,
                        "games": len(aligned),
                        "mae": round(float(arrays["ae"].mean()), 3),
                        "rmse": round(float(np.sqrt(arrays["se"].mean())), 3),
                        "crps": round(float(arrays["crps"].mean()), 3),
                        "log_likelihood": round(float(arrays["ll"].mean()), 4),
                        "ll_gain_vs_baseline": _interval(
                            bootstrap_difference(games, arrays["ll"] - base_arrays["ll"])
                        ),
                        "crps_gain_vs_baseline": _interval(
                            bootstrap_difference(games, base_arrays["crps"] - arrays["crps"])
                        ),
                    }
                )
    return rows


def _settled(frame: pl.DataFrame) -> pl.DataFrame:
    return frame.filter(pl.col("outcome").is_not_null() & pl.col("p_calibrated").is_not_null())


def market_table(calibrated: pl.DataFrame) -> list[dict[str, Any]]:
    """Log loss and Brier per family and line source, each candidate against the baseline."""
    rows: list[dict[str, Any]] = []
    settled = _settled(calibrated)
    for family in FAMILIES:
        for source in (REAL, SYNTHETIC):
            group = settled.filter((pl.col("family") == family) & (pl.col("source") == source))
            if group.is_empty():
                continue
            base = group.filter(pl.col("model") == BASELINE).select(
                *PAIR_KEYS,
                pl.col("p").alias("p_base"),
                pl.col("p_calibrated").alias("p_base_calibrated"),
            )
            for (model,), candidate in group.group_by(["model"], maintain_order=True):
                paired = candidate.join(base, on=PAIR_KEYS, how="inner")
                if paired.is_empty():
                    continue
                outcome = paired["outcome"].to_numpy()
                games = paired["game_id"].to_numpy()
                raw = log_loss(paired["p"].to_numpy(), outcome)
                cal = log_loss(paired["p_calibrated"].to_numpy(), outcome)
                raw_base = log_loss(paired["p_base"].to_numpy(), outcome)
                cal_base = log_loss(paired["p_base_calibrated"].to_numpy(), outcome)
                # The baseline at its best: a calibrator that hurt it mustn't flatter the model.
                best_base = raw_base if raw_base.mean() < cal_base.mean() else cal_base
                rows.append(
                    {
                        "family": family,
                        "source": source,
                        "model": model,
                        "rows": len(paired),
                        "log_loss_raw": round(float(raw.mean()), 5),
                        "log_loss_calibrated": round(float(cal.mean()), 5),
                        "brier_calibrated": round(
                            float(brier(paired["p_calibrated"].to_numpy(), outcome).mean()), 5
                        ),
                        "gain_raw_vs_baseline": _interval(
                            bootstrap_difference(games, raw_base - raw)
                        ),
                        "gain_calibrated_vs_baseline": _interval(
                            bootstrap_difference(games, cal_base - cal)
                        ),
                        "gain_vs_best_baseline": _interval(
                            bootstrap_difference(games, best_base - cal)
                        ),
                    }
                )
    return rows


def gates(markets: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Docs/05's gate: open only if the calibrated model beats baseline 1 on log loss.

    The baseline is taken raw or calibrated, whichever scored better (D61).

    A family with real lines is judged on them (D17). The rest are judged on the shared synthetic
    lines, and even if open, any claim of an edge over the market stays unproven (D18).
    """
    result: dict[str, dict[str, Any]] = {}
    for family in FAMILIES:
        source = REAL if family in REAL_FAMILIES else SYNTHETIC
        row = next(
            (
                r for r in markets
                if r["family"] == family and r["source"] == source and r["model"] == MODEL
            ),
            None,
        )  # fmt: skip
        if row is None:
            result[family] = {
                "enabled": False,
                "judged_on": source,
                "gain": None,
                "reason": "No settled rows to judge",
                "edge": "unproven",
            }
            continue
        gain = row["gain_vs_best_baseline"]
        passed = gate_decision(gain["low"], gain["high"])
        result[family] = {
            "enabled": passed,
            "judged_on": source,
            "gain": gain,
            "reason": "beats baselines with CI excluding zero"
            if passed
            else "does not beat baselines",
            "edge": "measured" if source == REAL else "unproven",
        }
    return result


def apply_overrides(
    decisions: dict[str, dict[str, Any]], overrides: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """The owner may overrule a gate (docs/05), but the evidence's own answer stays on record."""
    for family, override in overrides.items():
        if family not in decisions:
            raise ValueError(f"Override for unknown market {family}")
        if not override.get("decision"):
            raise ValueError(f"Override for {family} names no logged decision")
    return {
        family: {
            **gate,
            "passed": gate["enabled"],
            "enabled": overrides[family]["enabled"] if family in overrides else gate["enabled"],
            "override": overrides.get(family),
        }
        for family, gate in decisions.items()
    }


def reliability(calibrated: pl.DataFrame) -> list[dict[str, Any]]:
    """Ten equal probability bins for the model, on calibrated probabilities (both sides merged)."""
    rows: list[dict[str, Any]] = []
    settled = _settled(calibrated).filter(pl.col("model") == MODEL)
    for (family, source), group in settled.group_by(["family", "source"], maintain_order=True):
        p = group["p_calibrated"].to_numpy()
        outcome = group["outcome"].to_numpy()
        which = np.clip(np.digitize(p, BUCKET_EDGES) - 1, 0, 9)
        for bucket in range(10):
            chosen = which == bucket
            if not chosen.any():
                continue
            rows.append(
                {
                    "family": family,
                    "source": source,
                    "low": round(float(BUCKET_EDGES[bucket]), 1),
                    "high": round(float(BUCKET_EDGES[bucket + 1]), 1),
                    "rows": int(chosen.sum()),
                    "mean_p": round(float(p[chosen].mean()), 4),
                    "hit_rate": round(float(outcome[chosen].mean()), 4),
                }
            )
    return rows


def both_sides(calibrated: pl.DataFrame, source: str = REAL) -> pl.DataFrame:
    """Add the other side of each row, with its fair probability (50% with no prices) and edge."""
    rows = _settled(calibrated).filter(pl.col("source") == source)
    if rows.is_empty():
        return rows.with_columns(pl.lit(0.0).alias("edge"), pl.lit(True).alias("is_home_side"))
    price = rows["price"].to_numpy(allow_copy=True).astype(float)
    other = rows["price_other"].to_numpy(allow_copy=True).astype(float)
    known = np.isfinite(price) & np.isfinite(other) & (price > 1.0) & (other > 1.0)
    fair = np.full(len(rows), 0.5)
    fair[known] = no_vig_probability(price[known], other[known])
    with_fair = rows.with_columns(pl.Series("fair", fair))
    first = with_fair.with_columns(
        (pl.col("p_calibrated") - pl.col("fair")).alias("edge"), pl.lit(True).alias("is_home_side")
    )
    second = with_fair.with_columns(
        (1.0 - pl.col("p_calibrated")).alias("p_calibrated"),
        (1.0 - pl.col("outcome")).alias("outcome"),
        pl.col("price_other").alias("price"),
        pl.col("price").alias("price_other"),
        (1.0 - pl.col("p_calibrated") - (1.0 - pl.col("fair"))).alias("edge"),
        pl.lit(False).alias("is_home_side"),
    )
    return pl.concat([first, second])


def selection_table(
    calibrated: pl.DataFrame, thresholds: dict[str, dict[str, float]]
) -> list[dict[str, Any]]:
    """Picks passing the thresholds on real lines: hit rate, and flat-stake return at -110."""
    rows: list[dict[str, Any]] = []
    sides = both_sides(calibrated)
    if sides.is_empty():
        return rows
    for family in REAL_FAMILIES:
        chosen_thresholds = thresholds.get(family)
        if chosen_thresholds is None:
            continue
        for (model,), group in sides.filter(pl.col("family") == family).group_by(
            ["model"], maintain_order=True
        ):
            picks = group.filter(
                (pl.col("p_calibrated") >= chosen_thresholds["min_prob"])
                & (pl.col("edge") >= chosen_thresholds["min_edge"])
            )
            if picks.is_empty():
                rows.append({"family": family, "model": model, "picks": 0})
                continue
            outcome = picks["outcome"].to_numpy()
            games = picks["game_id"].to_numpy()
            returns = flat_stake_return(outcome)
            rows.append(
                {
                    "family": family,
                    "model": model,
                    "picks": len(picks),
                    "hit_rate": _interval(bootstrap_difference(games, outcome)),
                    "mean_p": round(float(picks["p_calibrated"].to_numpy().mean()), 4),
                    "flat_return_at_minus_110": _interval(bootstrap_difference(games, returns)),
                }
            )
    return rows


def market_line_table(predictions: pl.DataFrame, lines: pl.DataFrame) -> list[dict[str, Any]]:
    """Each candidate's implied line against the market's, on the games with a closing line."""
    rows: list[dict[str, Any]] = []
    full = predictions.filter(pl.col("period") == "ft").join(lines, on="game_id", how="inner")
    for (model,), group in full.group_by(["model"], maintain_order=True):
        actual_margin = (group["actual_home"] - group["actual_away"]).to_numpy()
        actual_total = (group["actual_home"] + group["actual_away"]).to_numpy()
        margin = (group["mu_home"] - group["mu_away"]).to_numpy()
        total = (group["mu_home"] + group["mu_away"]).to_numpy()
        market_margin = -group["spread_home"].to_numpy()
        market_total = group["total"].to_numpy()
        games = group["game_id"].to_numpy()
        rows.append(
            {
                "model": model,
                "games": len(group),
                "margin_mae": round(float(np.abs(actual_margin - margin).mean()), 3),
                "market_margin_mae": round(float(np.abs(actual_margin - market_margin).mean()), 3),
                "margin_gain_over_market": _interval(
                    bootstrap_difference(
                        games,
                        np.abs(actual_margin - market_margin) - np.abs(actual_margin - margin),
                    )
                ),
                "total_mae": round(float(np.abs(actual_total - total).mean()), 3),
                "market_total_mae": round(float(np.abs(actual_total - market_total).mean()), 3),
                "total_gain_over_market": _interval(
                    bootstrap_difference(
                        games, np.abs(actual_total - market_total) - np.abs(actual_total - total)
                    )
                ),
            }
        )
    return rows


def early_season_table(
    predictions: pl.DataFrame, game_ids: set[str] | None
) -> list[dict[str, Any]]:
    """Error by how few games the less-seen team had played, and the season's first two weeks."""
    rows: list[dict[str, Any]] = []
    full = predictions.filter(pl.col("period") == "ft")
    if game_ids is not None:
        full = full.filter(pl.col("game_id").is_in(sorted(game_ids)))
    fewest = pl.min_horizontal("games_home", "games_away")
    groups = [
        (label, full.filter((fewest >= low) & (fewest <= high)))
        for label, low, high in EARLY_BUCKETS
    ]
    first_days = full.filter(pl.col("days_since_start") < FIRST_DAYS)
    groups.append((f"first {FIRST_DAYS} days", first_days))
    for label, subset in groups:
        for (model,), group in subset.group_by(["model"], maintain_order=True):
            margin_error = (
                (group["actual_home"] - group["actual_away"])
                - (group["mu_home"] - group["mu_away"])
            ).to_numpy()
            total_error = (
                (group["actual_home"] + group["actual_away"])
                - (group["mu_home"] + group["mu_away"])
            ).to_numpy()
            rows.append(
                {
                    "bucket": label,
                    "model": model,
                    "games": len(group),
                    "margin_mae": round(float(np.abs(margin_error).mean()), 3),
                    "margin_bias": round(float(margin_error.mean()), 3),
                    "total_mae": round(float(np.abs(total_error).mean()), 3),
                    "total_bias": round(float(total_error.mean()), 3),
                }
            )
    return rows


def calibrate_by_window(scored: pl.DataFrame, starts: list[date], end: date) -> pl.DataFrame:
    """Each window's calibrators use only slates before it, as nightly refreshes would."""
    boundaries = [*starts[1:], end]
    parts = [
        with_calibration(scored, start, stop)
        for start, stop in zip(starts, boundaries, strict=True)
    ]
    return pl.concat([part for part in parts if len(part)])
