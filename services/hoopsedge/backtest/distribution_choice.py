"""Normal against empirical residuals, and an explicit overtime mixture (docs/05 section 4.1).

Both alternatives are judged on validation rows only: the first half supplies their shapes and
the second half scores them. An alternative replaces the normal only if the interval on its
improvement sits above zero. Otherwise the simpler form stays.
"""

from typing import Any

import numpy as np
import polars as pl
from scipy.stats import norm

from hoopsedge.backtest.metrics import bootstrap_difference
from hoopsedge.models.distribution import crps_normal, log_likelihood_normal

QUANTILES = np.linspace(0.005, 0.995, 199)


def crps_empirical(y: np.ndarray, mu: np.ndarray, sd: np.ndarray, shape: np.ndarray) -> np.ndarray:
    """CRPS when the forecast is `mu + sd * z`, with `z` drawn from equal-weight `shape` points."""
    points = mu[:, None] + sd[:, None] * shape[None, :]
    first = np.abs(points - y[:, None]).mean(axis=1)
    spread = float(np.abs(shape[:, None] - shape[None, :]).mean())
    result: np.ndarray = first - 0.5 * sd * spread
    return result


def ot_mixture_log_likelihood(
    total: np.ndarray,
    mu_total: np.ndarray,
    sd_total: np.ndarray,
    mu_margin: np.ndarray,
    sd_margin: np.ndarray,
    extra_points: float,
) -> np.ndarray:
    """Regulation or overtime, with the overall mean unchanged because targets include overtime."""
    tie = norm.cdf((0.5 - mu_margin) / sd_margin) - norm.cdf((-0.5 - mu_margin) / sd_margin)
    regular = norm.pdf(total, mu_total - tie * extra_points, sd_total)
    overtime = norm.pdf(total, mu_total + (1 - tie) * extra_points, sd_total)
    result: np.ndarray = np.log(np.maximum((1 - tie) * regular + tie * overtime, 1e-300))
    return result


def _verdict(games: np.ndarray, gain: np.ndarray) -> dict[str, Any]:
    interval = bootstrap_difference(games, gain)
    return {
        "gain": round(interval.mean, 5),
        "low": round(interval.low, 5),
        "high": round(interval.high, 5),
        "adopt": interval.low > 0,
    }


def compare_forms(predictions: pl.DataFrame, overtime_extra: float) -> dict[str, Any]:
    """Compare on the model's full-game validation predictions, sorted by date."""
    rows = predictions.filter((pl.col("model") == "model") & (pl.col("period") == "ft")).sort(
        "slate_date_et", "game_id"
    )
    middle = len(rows) // 2
    first, second = rows[:middle], rows[middle:]

    def column(frame: pl.DataFrame, name: str) -> np.ndarray:
        return frame[name].to_numpy()

    def target(frame: pl.DataFrame, name: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        home, away = column(frame, "actual_home"), column(frame, "actual_away")
        mu_home, mu_away = column(frame, "mu_home"), column(frame, "mu_away")
        if name == "margin":
            return home - away, mu_home - mu_away, column(frame, "sd_margin")
        return home + away, mu_home + mu_away, column(frame, "sd_total")

    games = column(second, "game_id")
    result: dict[str, Any] = {"validation_games": len(rows), "empirical": {}}
    for name in ("margin", "total"):
        y_fit, mu_fit, sd_fit = target(first, name)
        shape = np.quantile((y_fit - mu_fit) / sd_fit, QUANTILES)
        y, mu, sd = target(second, name)
        gain = crps_normal(y, mu, sd) - crps_empirical(y, mu, sd, shape)
        result["empirical"][name] = _verdict(games, gain)

    total, mu_total, sd_total = target(second, "total")
    _, mu_margin, sd_margin = target(second, "margin")
    gain = ot_mixture_log_likelihood(
        total, mu_total, sd_total, mu_margin, sd_margin, overtime_extra
    ) - log_likelihood_normal(total, mu_total, sd_total)
    result["overtime_mixture"] = _verdict(games, gain)
    result["overtime_extra_points"] = round(overtime_extra, 2)
    adopt_empirical = all(item["adopt"] for item in result["empirical"].values())
    result["distribution"] = "empirical" if adopt_empirical else "normal"
    result["overtime"] = "mixture" if result["overtime_mixture"]["adopt"] else "absorbed"
    return result
