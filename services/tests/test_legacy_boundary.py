"""D96: the v1 model code lives in `services/legacy/`, and nothing active can reach it.

The brief (§1) asks for v1 to sit "behind a dead import path". These tests keep it dead: no
active module imports or launches it, the moved packages are gone from `hoopsedge`, and every
active module still imports on its own.
"""

import importlib
import pkgutil
import re
from pathlib import Path

import hoopsedge

SERVICES = Path(__file__).resolve().parent.parent
REPO = SERVICES.parent
ACTIVE = [
    *sorted((SERVICES / "hoopsedge").rglob("*.py")),
    *sorted((SERVICES / "tests").glob("*.py")),
    *sorted((REPO / "scripts").glob("*.py")),
]
MOVED_PACKAGES = (
    "ratings",
    "features",
    "models",
    "backtest",
    "learning",
    "markets",
    "engine",
    "teams",
)
MOVED_MODULES = (
    "pipeline.predict", "pipeline.slate", "pipeline.live_test", "pipeline.labels",
    "pipeline.store", "sportybet.offers", "sportybet.booking", "sportybet.verification",
    "grading.data", "grading.run", "ingest.backfill", "ingest.live", "ingest.collector",
    "ingest.curated", "ingest.espn_parser", "ingest.injuries", "common.thresholds",
)  # fmt: skip
REACHES_LEGACY = re.compile(
    r"\blegacy\.|from legacy\b|import legacy\b"
    rf"|hoopsedge\.({'|'.join(MOVED_PACKAGES)})\b"
    rf"|hoopsedge\.({'|'.join(re.escape(name) for name in MOVED_MODULES)})\b"
)


def test_the_rule_catches_what_it_should() -> None:
    for text in (
        "from hoopsedge.models.distribution import ScoreDistribution",
        '"hoopsedge.pipeline.slate",',
        "import legacy.hoopsedge.engine",
        "from hoopsedge.ingest.backfill import write_gz",
    ):
        assert REACHES_LEGACY.search(text), text
    for text in ("from hoopsedge.ingest.scores import SOURCES", "hoopsedge.pipeline.schedule"):
        assert not REACHES_LEGACY.search(text), text


def test_no_active_file_imports_or_launches_legacy_code() -> None:
    this_file = Path(__file__).resolve()
    hits = [
        f"{path.relative_to(REPO).as_posix()}:{number}: {line.strip()}"
        for path in ACTIVE
        if path.resolve() != this_file
        for number, line in enumerate(path.read_text("utf-8").splitlines(), 1)
        if REACHES_LEGACY.search(line)
    ]
    assert hits == []


def test_the_moved_code_is_gone_from_the_active_package() -> None:
    package = SERVICES / "hoopsedge"
    for name in MOVED_PACKAGES:
        assert not (package / name).exists(), name
    for name in MOVED_MODULES:
        assert not (package / f"{name.replace('.', '/')}.py").exists(), name
    assert (SERVICES / "legacy" / "hoopsedge" / "models").is_dir()  # Kept, not deleted (brief §1).


def test_every_active_module_imports() -> None:
    names = [
        module.name for module in pkgutil.walk_packages(hoopsedge.__path__, prefix="hoopsedge.")
    ]
    assert "hoopsedge.v2.slate" in names and "hoopsedge.l5.model" in names
    for name in names:
        importlib.import_module(name)
