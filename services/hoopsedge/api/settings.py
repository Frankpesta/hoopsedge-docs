"""The settings document (docs/00 section 6), seeded from what Phase 4 decided.

Thresholds are per league, unlike docs/00's single set, because Phase 4 chose them per league (D60).
`passed_gate` and `edge_label` aren't settings: the evidence decides those, so they can't be edited.
"""

from pathlib import Path
from typing import Any

import yaml

from hoopsedge.common.thresholds import thresholds_in_force

PARAMS_DIR = Path(__file__).parent.parent / "models" / "params"
FAMILIES = ("handicap", "total_ft", "total_1h", "team_total")
LEAGUES = ("NBA", "NCAA")
SCHEDULE_NAMES = ("ncaa_slate", "nba_slate", "nba_late_slate", "nightly_learn")


def default_settings(params_dir: Path = PARAMS_DIR) -> dict[str, Any]:
    gates = yaml.safe_load((params_dir / "market_gates.yaml").read_text("utf-8"))
    thresholds = thresholds_in_force(params_dir)
    markets: dict[str, dict[str, Any]] = {}
    for league in LEAGUES:
        markets[league] = {}
        for family in FAMILIES:
            chosen = thresholds[league][family]
            markets[league][family] = {
                "enabled": gates[league][family]["enabled"],
                "min_prob": chosen["min_prob"],
                "min_edge": chosen["min_edge"],
            }
    return {
        "schedules": {
            "ncaa_slate": "15:00",
            "nba_slate": "22:00",
            "nba_late_slate": "00:30",
            "nightly_learn": "11:00",
        },
        "leagues": {league: {"enabled": True} for league in LEAGUES},
        "markets": markets,
        "min_legs": 2,
        "min_minutes_before_tip": 20,
        "allow_alt_lines": False,
        "sportybet_proxy_enabled": False,
        # C12: true until someone turns it off on purpose. The environment can still force it on.
        "dry_run": True,
        "pause_all": False,
    }


def gate_verdicts(params_dir: Path = PARAMS_DIR) -> dict[str, Any]:
    gates: dict[str, Any] = yaml.safe_load((params_dir / "market_gates.yaml").read_text("utf-8"))
    return {
        league: {
            family: {
                "passed": gate["passed"],
                "edge": gate["edge"],
                "reason": gate["reason"],
                "override": gate.get("override"),
            }
            for family, gate in group.items()
        }
        for league, group in gates.items()
    }


def changes_between(old: Any, new: Any, path: str = "") -> list[dict[str, Any]]:
    """Every value that differs, so the audit and the diff preview say exactly what changed."""
    if isinstance(old, dict) and isinstance(new, dict):
        found: list[dict[str, Any]] = []
        for key in sorted(old.keys() | new.keys()):
            found += changes_between(old.get(key), new.get(key), f"{path}.{key}" if path else key)
        return found
    return [] if old == new else [{"path": path, "from": old, "to": new}]
