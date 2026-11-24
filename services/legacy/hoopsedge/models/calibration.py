"""Per league and market-family probability calibrators (docs/05 section 4.3).

Fit only on out-of-sample predictions. Stored as plain JSON so they can refresh on their own
schedule, separately from the mean and variance models.
"""

from dataclasses import dataclass
from typing import Any

import numpy as np
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression

ISOTONIC_MIN_SAMPLES = 1000
MIN_SAMPLES = 100
MIN_BLOCK = 50
EPSILON = 1e-3
UNREGULARIZED = 1e6


def _logit(p: np.ndarray) -> np.ndarray:
    clipped = np.clip(p, EPSILON, 1 - EPSILON)
    return np.asarray(np.log(clipped / (1 - clipped)))


def _isotonic_knots(
    raw: np.ndarray, outcome: np.ndarray
) -> tuple[tuple[float, ...], tuple[float, ...]]:
    """Isotonic fit whose every step rests on at least `MIN_BLOCK` rows.

    A plain fit gives a step to a handful of rows at the edge of the range, where a lucky run
    becomes a near-certain probability. Small steps merge into a neighbour, and each rate is
    shrunk a little toward 50%, so even an all-wins step never claims certainty.
    """
    order = np.argsort(raw, kind="stable")
    x_sorted, y_sorted = raw[order], outcome[order]
    fitted = IsotonicRegression(out_of_bounds="clip").fit_transform(x_sorted, y_sorted)
    edges = np.flatnonzero(np.diff(fitted) > 0) + 1
    blocks = [list(pair) for pair in zip(np.r_[0, edges], np.r_[edges, len(fitted)], strict=True)]
    while len(blocks) > 1:
        sizes = [end - start for start, end in blocks]
        smallest = int(np.argmin(sizes))
        if sizes[smallest] >= MIN_BLOCK:
            break
        if smallest == 0:
            partner = 1
        elif smallest == len(blocks) - 1:
            partner = smallest - 1
        else:
            partner = smallest - 1 if sizes[smallest - 1] <= sizes[smallest + 1] else smallest + 1
        low, high = sorted((smallest, partner))
        blocks[low : high + 1] = [[blocks[low][0], blocks[high][1]]]
    rates = [
        (float(y_sorted[start:end].sum()) + 1.0) / (end - start + 2.0) for start, end in blocks
    ]
    knots = [float(x_sorted[start:end].mean()) for start, end in blocks]
    return tuple(knots), tuple(float(v) for v in np.maximum.accumulate(rates))


@dataclass(frozen=True)
class Calibrator:
    kind: str
    # Isotonic: knots `x` and values `y`. Platt: `x` is [slope], `y` is [intercept].
    x: tuple[float, ...]
    y: tuple[float, ...]
    samples: int

    @classmethod
    def fit(cls, raw: np.ndarray, outcome: np.ndarray) -> "Calibrator":
        count = len(raw)
        if count < MIN_SAMPLES:
            raise ValueError(f"{count} samples is too few to calibrate; {MIN_SAMPLES} are needed")
        if len(np.unique(outcome)) < 2:
            raise ValueError("Calibration needs both outcomes in the sample")
        if count >= ISOTONIC_MIN_SAMPLES:
            x, y = _isotonic_knots(raw, outcome)
            return cls("isotonic", x, y, count)
        platt = LogisticRegression(C=UNREGULARIZED)
        platt.fit(_logit(raw).reshape(-1, 1), outcome)
        slope = float(platt.coef_[0, 0])
        if slope <= 0:
            # A noisy sample can fit a falling line, which would reverse the ranking. With no
            # usable signal, the honest answer is the base rate for every input.
            return cls("platt", (0.0,), (float(_logit(np.array([outcome.mean()]))[0]),), count)
        return cls("platt", (slope,), (float(platt.intercept_[0]),), count)

    def apply(self, raw: np.ndarray) -> np.ndarray:
        if self.kind == "isotonic":
            return np.asarray(np.interp(raw, self.x, self.y))
        score = self.x[0] * _logit(raw) + self.y[0]
        return np.asarray(np.clip(1.0 / (1.0 + np.exp(-score)), EPSILON, 1 - EPSILON))

    def to_json(self) -> dict[str, Any]:
        return {"kind": self.kind, "x": list(self.x), "y": list(self.y), "samples": self.samples}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Calibrator":
        return cls(data["kind"], tuple(data["x"]), tuple(data["y"]), data["samples"])
