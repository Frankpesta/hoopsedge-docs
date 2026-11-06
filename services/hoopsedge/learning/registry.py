"""Which model is in force, and every decision about it (docs/08 section 7.4).

The pointer lives in DynamoDB. Locally the stand-in forgets everything when it restarts, so a copy
is kept beside the models and restores it. S3 takes that role with Phase 1. Nothing here deletes a
version, so a rollback can always go back.
"""

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from boto3.dynamodb.conditions import Key

from hoopsedge.features.registry import FEATURE_SET_VERSION
from hoopsedge.models.calibration import Calibrator
from hoopsedge.models.registry import load_calibrators
from hoopsedge.pipeline.store import to_item

REAL = "real"
SYNTHETIC = "synthetic"
# D18, D72: which lines each family's calibrators have actually seen.
STARTING_SOURCES = {
    "handicap": REAL,
    "total_ft": REAL,
    "total_1h": SYNTHETIC,
    "team_total": SYNTHETIC,
}
DIRTY_SUFFIX = "-dirty"
CALIBRATOR_VERSIONS = "calibrator_versions"


class RegistryError(Exception):
    """A request the registry refuses, such as rolling back to a version that doesn't exist."""


@dataclass(frozen=True)
class Champion:
    version: str
    calibrator_version: str
    promoted_at: str
    reason: str
    cutoff: str  # Nothing the model trained on tipped after this (D16).
    calibrator_sources: dict[str, str] = field(default_factory=lambda: dict(STARTING_SOURCES))


def version_time(version: str) -> datetime:
    """When a version was built, from its name: `{yyyymmdd-hhmm}-{gitsha[:7]}`."""
    return datetime.strptime(version[:13], "%Y%m%d-%H%M").replace(tzinfo=UTC)


class ModelRegistry:
    def __init__(self, table: Any, root: Path) -> None:
        self.table = table
        self.root = root

    def _mirror(self, league: str) -> Path:
        return self.root / league / "champion.json"

    def _write_pointer(self, league: str, champion: Champion) -> None:
        self.table.put_item(
            Item=to_item({"pk": f"CHAMPION#{league}", "sk": "CURRENT", **asdict(champion)})
        )
        path = self._mirror(league)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(champion), indent=1), "utf-8")

    def champion(self, league: str) -> Champion | None:
        item = self.table.get_item(Key={"pk": f"CHAMPION#{league}", "sk": "CURRENT"}).get("Item")
        if item is not None:
            fields = {name: item[name] for name in Champion.__dataclass_fields__}
            return Champion(**fields)
        mirror = self._mirror(league)
        if not mirror.exists():
            return None
        champion = Champion(**json.loads(mirror.read_text("utf-8")))
        self._write_pointer(league, champion)
        return champion

    def require(self, league: str) -> Champion:
        champion = self.champion(league)
        if champion is None:
            raise RegistryError(
                f"No champion for {league}: run `learning.run bootstrap --league {league}`"
            )
        return champion

    def audit(
        self, league: str, now: datetime, version: str, decision: str, details: dict[str, Any]
    ) -> None:
        self.table.put_item(
            Item=to_item(
                {
                    "pk": f"MODEL#{league}",
                    "sk": f"{now.astimezone(UTC).isoformat()}#{version}",
                    "version": version,
                    "decision": decision,
                    **details,
                }
            )
        )

    def history(self, league: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = self.table.query(
            KeyConditionExpression=Key("pk").eq(f"MODEL#{league}")
        )["Items"]
        return items

    def model_folder(self, league: str, version: str) -> Path:
        return self.root / league / version

    def _check_usable(self, league: str, version: str) -> Path:
        folder = self.model_folder(league, version)
        if not (folder / "periods.json").exists():
            raise RegistryError(f"{league} has no model version {version}")
        built_for = (folder / "feature_set_version.txt").read_text("utf-8").strip()
        if built_for != FEATURE_SET_VERSION:
            raise RegistryError(
                f"{version} was built for features {built_for}, and they're now "
                f"{FEATURE_SET_VERSION}"
            )
        return folder

    def _calibrator_folder(self, league: str, champion: Champion) -> Path:
        if champion.calibrator_version == champion.version:
            return self.model_folder(league, champion.version)
        return self.root / league / CALIBRATOR_VERSIONS / champion.calibrator_version

    def calibrators(self, league: str, champion: Champion) -> dict[str, Calibrator]:
        return load_calibrators(self._calibrator_folder(league, champion))

    def bootstrap(self, league: str, now: datetime) -> Champion:
        """The first champion: the newest version built from committed code."""
        existing = self.champion(league)
        if existing is not None:
            return existing
        folders = sorted(
            path
            for path in (self.root / league).glob("*")
            if (path / "periods.json").exists() and not path.name.endswith(DIRTY_SUFFIX)
        )
        if not folders:
            raise RegistryError(
                f"No clean {league} version: run `backtest.run evidence` with HOOPSEDGE_GIT_SHA set"
            )
        version = folders[-1].name
        self._check_usable(league, version)
        champion = Champion(
            version=version,
            calibrator_version=version,
            promoted_at=now.isoformat(),
            reason="First champion: the newest clean evidence model",
            cutoff=version_time(version).isoformat(),
        )
        self._write_pointer(league, champion)
        self.audit(league, now, version, "bootstrap", {"reason": champion.reason})
        return champion

    def promote(
        self, league: str, version: str, calibrators_from: Champion, now: datetime, reason: str
    ) -> Champion:
        """A new model keeps the calibrators in force, which refresh on their own schedule."""
        if version.endswith(DIRTY_SUFFIX):
            raise RegistryError(
                f"{version} was built from uncommitted code, so it can't be promoted"
            )
        self._check_usable(league, version)
        target = self.model_folder(league, version) / "calibrators"
        target.mkdir(exist_ok=True)
        for name, calibrator in self.calibrators(league, calibrators_from).items():
            (target / f"{name}.json").write_text(json.dumps(calibrator.to_json()), "utf-8")
        champion = Champion(
            version=version,
            calibrator_version=version,
            promoted_at=now.isoformat(),
            reason=reason,
            cutoff=version_time(version).isoformat(),
            calibrator_sources=dict(calibrators_from.calibrator_sources),
        )
        self._write_pointer(league, champion)
        self.audit(league, now, version, "promoted", {"reason": reason})
        return champion

    def adopt_calibrators(
        self,
        league: str,
        champion: Champion,
        adopted: dict[str, Calibrator],
        sources: dict[str, str],
        now: datetime,
        details: dict[str, Any],
    ) -> Champion:
        """Calibrators can change nightly without a new model. Untouched families carry over."""
        name = f"{now:%Y%m%d-%H%M}-{champion.version[:13]}"
        target = self.root / league / CALIBRATOR_VERSIONS / name / "calibrators"
        target.mkdir(parents=True, exist_ok=True)
        merged = {**self.calibrators(league, champion), **adopted}
        for family, calibrator in merged.items():
            (target / f"{family}.json").write_text(json.dumps(calibrator.to_json()), "utf-8")
        updated = Champion(
            version=champion.version,
            calibrator_version=name,
            promoted_at=champion.promoted_at,
            reason=champion.reason,
            cutoff=champion.cutoff,
            calibrator_sources={**champion.calibrator_sources, **sources},
        )
        self._write_pointer(league, updated)
        self.audit(league, now, name, "calibrators", details)
        return updated

    def rollback(self, league: str, version: str, now: datetime, reason: str) -> Champion:
        """Back to any version whose features still match, with that version's own calibrators."""
        self._check_usable(league, version)
        if not (self.model_folder(league, version) / "calibrators").exists():
            raise RegistryError(f"{version} has no calibrators of its own")
        champion = Champion(
            version=version,
            calibrator_version=version,
            promoted_at=now.isoformat(),
            reason=reason,
            cutoff=version_time(version).isoformat(),
        )
        self._write_pointer(league, champion)
        self.audit(league, now, version, "rollback", {"reason": reason})
        return champion
