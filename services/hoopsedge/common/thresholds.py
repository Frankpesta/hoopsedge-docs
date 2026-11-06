"""The thresholds in force: what testing chose, with the owner's overrides on top (D78).

Only needs YAML, so the API can read it without loading the model libraries.
"""

from pathlib import Path
from typing import Any

import yaml

PARAMS_DIR = Path(__file__).parent.parent / "models" / "params"
LEAGUES = ("NBA", "NCAA")
FAMILIES = ("handicap", "total_ft", "total_1h", "team_total")


def _read(path: Path) -> dict[str, dict[str, dict[str, Any] | None]]:
    if not path.exists():
        raise FileNotFoundError(f"No market settings at {path}: run the Phase 4 evidence first")
    loaded: dict[str, dict[str, dict[str, Any] | None]] = yaml.safe_load(path.read_text("utf-8"))
    return loaded


def thresholds_in_force(params_dir: Path = PARAMS_DIR) -> dict[str, dict[str, dict[str, Any]]]:
    """A market testing found no threshold for is an explicit null. No overrides file is fine."""
    tested = _read(params_dir / "thresholds.yaml")
    path = params_dir / "threshold_overrides.yaml"
    overrides = _read(path) if path.exists() else {}
    chosen: dict[str, dict[str, dict[str, Any]]] = {}
    for league in LEAGUES:
        chosen[league] = {}
        for family in FAMILIES:
            group = tested.get(league, {})
            if family not in group:
                raise ValueError(f"{league} {family} is missing from thresholds")
            entry = group[family] or {}
            override = overrides.get(league, {}).get(family) or {}
            chosen[league][family] = {
                "min_prob": override.get("min_prob", entry.get("min_prob")),
                "min_edge": override.get("min_edge", entry.get("min_edge")),
                "tested_min_prob": entry.get("min_prob"),
                "tested_min_edge": entry.get("min_edge"),
            }
    return chosen
