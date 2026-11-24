"""Which games tune, which warm up the calibrators, and which are the evidence (D60).

Docs/05 tests the last three seasons and validates on the season before. NBA can do that. NCAA's
history starts at 2023-24 (D66), so it trains on that season, validates on 2024-25, and tests what
follows. Validation always comes before any tested game, so tuning never sees the evidence.
"""

from dataclasses import dataclass, field
from datetime import date

from hoopsedge.backtest.walkforward import Window, season_windows


@dataclass(frozen=True)
class EvidenceWindow:
    label: str
    first: date
    last: date


@dataclass(frozen=True)
class Plan:
    league: str
    validation: Window
    tests: tuple[EvidenceWindow, ...]
    refit_days: int
    min_legs_per_slate: float
    family_leg_floors: dict[str, float] = field(default_factory=dict)

    def leg_floor(self, family: str) -> float:
        return self.family_leg_floors.get(family, self.min_legs_per_slate)

    def validation_windows(self) -> list[Window]:
        last = date.fromordinal(self.validation.end.toordinal() - 1)
        return season_windows(self.validation.start, last, self.refit_days)

    def test_windows(self, latest: date) -> list[tuple[str, Window]]:
        """Every refit window, labelled by evidence period. Stops at the newest game."""
        windows: list[tuple[str, Window]] = []
        for test in self.tests:
            last = min(test.last, latest)
            if last >= test.first:
                windows += [
                    (test.label, window)
                    for window in season_windows(test.first, last, self.refit_days)
                ]
        return windows


PLANS = {
    "NBA": Plan(
        "NBA",
        validation=Window(date(2022, 10, 1), date(2023, 7, 1)),
        tests=(
            EvidenceWindow("2023-24", date(2023, 10, 1), date(2024, 6, 30)),
            EvidenceWindow("2024-25", date(2024, 10, 1), date(2025, 6, 30)),
            EvidenceWindow("2025-26", date(2025, 10, 1), date(2026, 6, 30)),
            EvidenceWindow("2026-27 start", date(2026, 10, 1), date(2027, 6, 30)),
        ),
        refit_days=30,
        min_legs_per_slate=2.0,
    ),
    "NCAA": Plan(
        "NCAA",
        validation=Window(date(2024, 10, 1), date(2025, 5, 1)),
        tests=(
            EvidenceWindow("2025-26", date(2025, 10, 1), date(2026, 4, 30)),
            EvidenceWindow("2026-27 start", date(2026, 10, 1), date(2027, 4, 30)),
        ),
        refit_days=14,
        min_legs_per_slate=10.0,
        # Owner's decision (D69), knowing these picks hit 39-43% on tested games.
        family_leg_floors={"handicap": 1.0, "total_ft": 1.0},
    ),
}
