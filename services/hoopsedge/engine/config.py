"""Engine settings: market gates and thresholds from Phase 4's files, plus docs/00's defaults.

Reads files, so it stays out of `build.py`. A missing file or market stops the load (invariant 7):
a market with no threshold is written as an explicit null by Phase 4, never left out.
"""

from pathlib import Path
from typing import Any

import yaml

from hoopsedge.engine.schemas import FAMILIES, EngineConfig, LeagueSettings, MarketSettings
from hoopsedge.models.calibration import Calibrator

PARAMS_DIR = Path(__file__).parent.parent / "models" / "params"
LEAGUES = {
    "NBA": LeagueSettings(enabled=True, max_legs_per_ticket=20, max_tickets_per_market=1),
    "NCAA": LeagueSettings(
        enabled=True, max_legs_per_ticket=20, max_tickets_per_market=5, max_legs_per_market=100
    ),
}


def _read(path: Path) -> dict[str, dict[str, dict[str, Any] | None]]:
    if not path.exists():
        raise FileNotFoundError(f"No market settings at {path}: run the Phase 4 evidence first")
    loaded: dict[str, dict[str, dict[str, Any] | None]] = yaml.safe_load(path.read_text("utf-8"))
    return loaded


def _entry(table: dict[str, dict[str, Any] | None], name: str, league: str, family: str) -> Any:
    if family not in table:
        raise ValueError(f"{league} {family} is missing from {name}")
    return table[family]


def load_engine_config(
    calibrators: dict[str, dict[str, Calibrator]],
    params_dir: Path = PARAMS_DIR,
    **overrides: Any,
) -> EngineConfig:
    gates = _read(params_dir / "market_gates.yaml")
    thresholds = _read(params_dir / "thresholds.yaml")
    markets: dict[str, dict[str, MarketSettings]] = {}
    for league in LEAGUES:
        markets[league] = {}
        for family in FAMILIES:
            gate = _entry(gates.get(league, {}), "market_gates", league, family)
            chosen = _entry(thresholds.get(league, {}), "thresholds", league, family)
            markets[league][family] = MarketSettings(
                enabled=gate["enabled"],
                passed_gate=gate["passed"],
                edge_label=gate["edge"],
                min_prob=None if chosen is None else chosen["min_prob"],
                min_edge=None if chosen is None else chosen["min_edge"],
            )
    return EngineConfig(leagues=LEAGUES, markets=markets, calibrators=calibrators, **overrides)
