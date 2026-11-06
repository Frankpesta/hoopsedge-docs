"""Engine settings: market gates and thresholds from Phase 4's files, plus docs/00's defaults.

Reads files, so it stays out of `build.py`. A missing file or market stops the load (invariant 7):
a market with no threshold is written as an explicit null by Phase 4, never left out.
"""

from pathlib import Path
from typing import Any

import yaml

from hoopsedge.common.thresholds import thresholds_in_force
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


def apply_settings(config: EngineConfig, settings: dict[str, Any] | None) -> EngineConfig:
    """What the owner changed on the dashboard. Gate verdicts stay what the evidence said."""
    if not settings:
        return config
    markets = {name: dict(group) for name, group in config.markets.items()}
    for league, group in settings.get("markets", {}).items():
        for family, chosen in group.items():
            if family in markets.get(league, {}):
                markets[league][family] = markets[league][family].model_copy(
                    update={
                        "enabled": bool(chosen["enabled"]),
                        "min_prob": None
                        if chosen["min_prob"] is None
                        else float(chosen["min_prob"]),
                        "min_edge": None
                        if chosen["min_edge"] is None
                        else float(chosen["min_edge"]),
                    }
                )
    leagues = dict(config.leagues)
    for league, chosen in settings.get("leagues", {}).items():
        if league in leagues:
            leagues[league] = leagues[league].model_copy(
                update={"enabled": bool(chosen["enabled"])}
            )
    update: dict[str, Any] = {"markets": markets, "leagues": leagues}
    for name in ("min_legs", "min_minutes_before_tip"):
        if name in settings:
            update[name] = int(settings[name])
    if "allow_alt_lines" in settings:
        update["allow_alt_lines"] = bool(settings["allow_alt_lines"])
    return config.model_copy(update=update)


def with_calibrator_sources(
    config: EngineConfig, league: str, sources: dict[str, str]
) -> EngineConfig:
    """A market stops being `unproven` once its calibrators have really seen market lines (D72)."""
    markets = {name: dict(group) for name, group in config.markets.items()}
    for family, source in sources.items():
        if source == "real" and family in markets[league]:
            markets[league][family] = markets[league][family].model_copy(
                update={"edge_label": "measured"}
            )
    return config.model_copy(update={"markets": markets})


def load_engine_config(
    calibrators: dict[str, dict[str, Calibrator]],
    params_dir: Path = PARAMS_DIR,
    **overrides: Any,
) -> EngineConfig:
    gates = _read(params_dir / "market_gates.yaml")
    thresholds = thresholds_in_force(params_dir)
    markets: dict[str, dict[str, MarketSettings]] = {}
    for league in LEAGUES:
        markets[league] = {}
        for family in FAMILIES:
            gate = _entry(gates.get(league, {}), "market_gates", league, family)
            chosen = thresholds[league][family]
            markets[league][family] = MarketSettings(
                enabled=gate["enabled"],
                passed_gate=gate["passed"],
                edge_label=gate["edge"],
                min_prob=chosen["min_prob"],
                min_edge=chosen["min_edge"],
                has_tested_floor=True,
                tested_min_prob=chosen["tested_min_prob"],
                tested_min_edge=chosen["tested_min_edge"],
            )
    return EngineConfig(leagues=LEAGUES, markets=markets, calibrators=calibrators, **overrides)
