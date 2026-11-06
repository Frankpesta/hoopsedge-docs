"""Model artifacts on disk, in the registry layout of docs/08 section 7.4.

A family holds several boosters, so each gets its own file: `ft_mean.home.txt` rather than
`ft_mean.txt`. Versions are never overwritten. Locally the root is `data/models`.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import lightgbm as lgb
import polars as pl
import yaml

from hoopsedge.features.registry import FEATURE_SET_VERSION
from hoopsedge.models.training import ModelBundle, PeriodModel, training_data_hash


def model_version(now: datetime, git_sha: str, dirty: bool) -> str:
    """`{yyyymmdd-hhmm}-{gitsha[:7]}`, marked when built from uncommitted code (BUILD-PLAN)."""
    version = f"{now:%Y%m%d-%H%M}-{git_sha[:7]}"
    return f"{version}-dirty" if dirty else version


def _save_period(model: PeriodModel, folder: Path) -> dict[str, Any]:
    for family, group in (("mean", model.mean), ("var", model.var)):
        for name, booster in group.items():
            booster.save_model(str(folder / f"{model.period}_{family}.{name}.txt"))
    return {
        "layout": model.layout,
        "target": model.target,
        "mean": sorted(model.mean),
        "var": sorted(model.var),
        "scale": model.scale,
        "sd_bounds": {name: list(bound) for name, bound in model.sd_bounds.items()},
    }


def save_bundle(
    bundle: ModelBundle, root: Path, league: str, version: str, frame: pl.DataFrame
) -> Path:
    folder = root / league / version
    folder.mkdir(parents=True, exist_ok=False)  # Never overwrite a version (invariant 6).
    meta = {model.period: _save_period(model, folder) for model in (bundle.ft, bundle.h1)}
    (folder / "periods.json").write_text(json.dumps(meta, indent=1, sort_keys=True), "utf-8")
    (folder / "params.yaml").write_text(yaml.safe_dump(bundle.params, sort_keys=True), "utf-8")
    (folder / "feature_set_version.txt").write_text(FEATURE_SET_VERSION, "utf-8")
    (folder / "training_data_hash.txt").write_text(training_data_hash(frame), "utf-8")
    return folder


def _load_period(folder: Path, period: str, meta: dict[str, Any]) -> PeriodModel:
    def boosters(family: str) -> dict[str, lgb.Booster]:
        return {
            name: lgb.Booster(model_file=str(folder / f"{period}_{family}.{name}.txt"))
            for name in meta[family]
        }

    return PeriodModel(
        period,
        meta["layout"],
        meta["target"],
        boosters("mean"),
        boosters("var"),
        {name: float(value) for name, value in meta["scale"].items()},
        {name: (float(pair[0]), float(pair[1])) for name, pair in meta["sd_bounds"].items()},
    )


def load_bundle(folder: Path) -> ModelBundle:
    meta = json.loads((folder / "periods.json").read_text("utf-8"))
    params = yaml.safe_load((folder / "params.yaml").read_text("utf-8"))
    return ModelBundle(
        _load_period(folder, "ft", meta["ft"]), _load_period(folder, "h1", meta["h1"]), params
    )
