"""Promotion, the registry, recalibration, compaction, monitoring and the cycle (docs/08)."""

import gzip
import json
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import boto3
import numpy as np
import polars as pl
import pytest
from boto3.dynamodb.conditions import Key
from moto import mock_aws

from hoopsedge.backtest.metrics import Interval, bootstrap_difference
from hoopsedge.common.dynamo import ensure_table
from hoopsedge.engine import score_offers
from hoopsedge.engine.config import with_calibrator_sources
from hoopsedge.features.registry import FEATURE_SET_VERSION
from hoopsedge.grading.data import load_finals
from hoopsedge.grading.settle import Final
from hoopsedge.ingest.curated import compact_deltas, read_table
from hoopsedge.learning import collected as collected_module
from hoopsedge.learning.collected import last_lines, slates_to_score
from hoopsedge.learning.data import has_log, prediction_rows, scored_rows, write_log
from hoopsedge.learning.evaluate import holdout_metrics
from hoopsedge.learning.monitor import drift, error_report
from hoopsedge.learning.promotion import (
    BRIER_TOLERANCE,
    HOLDOUT_DAYS,
    decide,
    fair_to_compare,
)
from hoopsedge.learning.recalibrate import (
    MIN_REAL_ROWS_WITHOUT_TOP_UP,
    recalibrate,
    recalibrate_family,
)
from hoopsedge.learning.registry import (
    Champion,
    ModelRegistry,
    RegistryError,
    version_time,
)
from hoopsedge.learning.run import Learning, learn, step_challenger, step_recalibrate
from hoopsedge.models.calibration import Calibrator
from hoopsedge.models.registry import save_bundle
from hoopsedge.models.training import DEFAULT_PARAMS, fit_bundle
from hoopsedge.pipeline.schedule import due_jobs, run_due
from hoopsedge.pipeline.store import PipelineStore
from tests.engine_data import IDENTITY, config, offer, period, prediction
from tests.model_data import feature_frame

FAMILY_NAMES = ("handicap", "total_ft", "total_1h", "team_total")
NOW = datetime(2026, 12, 10, 10, 0, tzinfo=UTC)
TODAY = date(2026, 12, 10)
GOOD = Interval(0.02, 0.01, 0.03, 500)
NEUTRAL_CRPS = Interval(0.01, 0.0, 0.02, 500)
FAST: dict[str, Any] = {
    **DEFAULT_PARAMS,
    "mean": {**DEFAULT_PARAMS["mean"], "n_estimators": 20},
    "var": {**DEFAULT_PARAMS["var"], "n_estimators": 10},
}
SHA = "abc1234def"


# Promotion, on synthetic metrics.


def test_a_clearly_better_challenger_is_promoted() -> None:
    decision = decide("NCAA", 500, GOOD, NEUTRAL_CRPS, {"handicap": 0.0})
    assert decision.promote


@pytest.mark.parametrize(("league", "games"), [("NBA", 149), ("NCAA", 399)])
def test_too_few_holdout_games_blocks_promotion(league: str, games: int) -> None:
    decision = decide(league, games, GOOD, NEUTRAL_CRPS, {})
    assert not decision.promote
    assert "needed" in decision.reasons[0]


@pytest.mark.parametrize(("league", "games"), [("NBA", 150), ("NCAA", 400)])
def test_exactly_enough_games_is_enough(league: str, games: int) -> None:
    assert decide(league, games, GOOD, NEUTRAL_CRPS, {}).promote


def test_a_single_market_getting_worse_blocks_promotion() -> None:
    changes = {"handicap": -0.01, "total_ft": BRIER_TOLERANCE + 0.0001}
    decision = decide("NCAA", 500, GOOD, NEUTRAL_CRPS, changes)
    assert not decision.promote
    assert "total_ft" in decision.reasons[0]


def test_a_market_within_the_brier_tolerance_does_not_block() -> None:
    assert decide("NCAA", 500, GOOD, NEUTRAL_CRPS, {"total_ft": BRIER_TOLERANCE}).promote


def test_no_mean_improvement_blocks_promotion_whatever_else_looks_good() -> None:
    flat = Interval(0.0, -0.01, 0.01, 500)
    assert not decide("NCAA", 500, flat, NEUTRAL_CRPS, {}).promote
    worse = Interval(-0.01, -0.02, 0.0, 500)
    assert not decide("NCAA", 500, worse, NEUTRAL_CRPS, {}).promote


def test_an_uncertain_gain_needs_crps_to_be_no_worse_within_tolerance() -> None:
    uncertain = Interval(0.01, -0.001, 0.02, 500)
    assert decide("NCAA", 500, uncertain, Interval(0.01, -0.001, 0.02, 500), {}).promote
    bad_crps = Interval(-0.02, -0.05, 0.01, 500)
    assert not decide("NCAA", 500, uncertain, bad_crps, {}).promote


def test_several_problems_are_all_reported() -> None:
    worse = Interval(-0.01, -0.02, 0.0, 10)
    decision = decide("NBA", 10, worse, NEUTRAL_CRPS, {"handicap": 0.01})
    assert len(decision.reasons) == 3


def test_a_champion_that_trained_on_the_holdout_cannot_be_compared() -> None:
    start = datetime(2026, 11, 19, tzinfo=UTC)
    assert fair_to_compare(start - timedelta(seconds=1), start)
    assert not fair_to_compare(start, start)
    assert not fair_to_compare(start + timedelta(days=1), start)


def test_the_bootstrap_interval_narrows_at_ninety_percent() -> None:
    games = np.arange(500)
    values = np.random.default_rng(1).normal(0, 1, 500)
    wide = bootstrap_difference(games, values)
    narrow = bootstrap_difference(games, values, level=0.90)
    assert wide.low < narrow.low
    assert narrow.high < wide.high


def test_the_nba_holdout_is_longer_for_c15() -> None:
    assert HOLDOUT_DAYS == {"NBA": 28, "NCAA": 21}


# The registry.


@pytest.fixture
def table() -> Iterator[Any]:
    with mock_aws():
        resource = boto3.resource(
            "dynamodb", region_name="us-east-1",
            aws_access_key_id="x", aws_secret_access_key="x",  # noqa: S106 - Dummy values.
        )  # fmt: skip
        ensure_table(resource, "t")
        yield resource.Table("t")


def _version(
    root: Path, name: str, league: str = "NBA", features: str = FEATURE_SET_VERSION
) -> Path:
    folder = root / league / name
    (folder / "calibrators").mkdir(parents=True)
    (folder / "periods.json").write_text("{}", "utf-8")
    (folder / "feature_set_version.txt").write_text(features, "utf-8")
    for family in ("handicap", "total_ft", "total_1h", "team_total"):
        (folder / "calibrators" / f"{family}.json").write_text(
            json.dumps(IDENTITY.to_json()), "utf-8"
        )
    return folder


def _registry(table: Any, tmp_path: Path) -> ModelRegistry:
    return ModelRegistry(table, tmp_path)


def test_bootstrap_chooses_the_newest_clean_version(table: Any, tmp_path: Path) -> None:
    _version(tmp_path, "20261106-1000-aaaaaaa")
    _version(tmp_path, "20261106-1100-bbbbbbb")
    _version(tmp_path, "20261106-1200-unknown-dirty")
    champion = _registry(table, tmp_path).bootstrap("NBA", NOW)
    assert champion.version == "20261106-1100-bbbbbbb"
    assert champion.calibrator_sources["total_1h"] == "synthetic"
    assert champion.calibrator_sources["handicap"] == "real"
    assert datetime.fromisoformat(champion.cutoff) == version_time(champion.version)


def test_bootstrap_refuses_when_only_dirty_versions_exist(table: Any, tmp_path: Path) -> None:
    _version(tmp_path, "20261106-1200-unknown-dirty")
    with pytest.raises(RegistryError, match="clean"):
        _registry(table, tmp_path).bootstrap("NBA", NOW)


def test_bootstrap_never_replaces_a_champion(table: Any, tmp_path: Path) -> None:
    _version(tmp_path, "20261106-1000-aaaaaaa")
    registry = _registry(table, tmp_path)
    first = registry.bootstrap("NBA", NOW)
    _version(tmp_path, "20261107-1000-bbbbbbb")
    assert registry.bootstrap("NBA", NOW) == first


def test_no_champion_is_a_loud_error(table: Any, tmp_path: Path) -> None:
    with pytest.raises(RegistryError, match="bootstrap"):
        _registry(table, tmp_path).require("NBA")


def test_a_promotion_round_trips_with_the_calibrators_in_force(table: Any, tmp_path: Path) -> None:
    _version(tmp_path, "20261106-1000-aaaaaaa")
    registry = _registry(table, tmp_path)
    first = registry.bootstrap("NBA", NOW)
    better = Calibrator("platt", (0.5,), (0.1,), 500)
    with_new = registry.adopt_calibrators(
        "NBA", first, {"handicap": better}, {"handicap": "real"}, NOW, {}
    )
    folder = _version(tmp_path, "20261107-1000-bbbbbbb")
    (folder / "calibrators/handicap.json").unlink()
    promoted = registry.promote("NBA", "20261107-1000-bbbbbbb", with_new, NOW, "Better")
    assert registry.require("NBA") == promoted
    assert registry.calibrators("NBA", promoted)["handicap"] == better
    assert promoted.calibrator_sources == with_new.calibrator_sources


def test_a_dirty_version_can_never_be_promoted(table: Any, tmp_path: Path) -> None:
    _version(tmp_path, "20261106-1000-aaaaaaa")
    _version(tmp_path, "20261107-1000-unknown-dirty")
    registry = _registry(table, tmp_path)
    first = registry.bootstrap("NBA", NOW)
    with pytest.raises(RegistryError, match="uncommitted"):
        registry.promote("NBA", "20261107-1000-unknown-dirty", first, NOW, "No")
    assert registry.require("NBA") == first


def test_rollback_repoints_the_champion_and_is_audited(table: Any, tmp_path: Path) -> None:
    _version(tmp_path, "20261106-1000-aaaaaaa")
    registry = _registry(table, tmp_path)
    first = registry.bootstrap("NBA", NOW)
    _version(tmp_path, "20261107-1000-bbbbbbb")
    registry.promote("NBA", "20261107-1000-bbbbbbb", first, NOW, "Better")
    back = registry.rollback("NBA", first.version, NOW + timedelta(hours=1), "It looks off")
    assert registry.require("NBA").version == first.version
    assert back.reason == "It looks off"
    decisions = [item["decision"] for item in registry.history("NBA")]
    assert decisions == ["bootstrap", "promoted", "rollback"]


@pytest.mark.parametrize(
    ("version", "text"),
    [("20260101-0000-nothere", "no model version"), ("20261105-0000-oldfeat", "features")],
)
def test_rollback_refuses_what_cannot_run(
    table: Any, tmp_path: Path, version: str, text: str
) -> None:
    _version(tmp_path, "20261105-0000-oldfeat", features="old-features")
    with pytest.raises(RegistryError, match=text):
        _registry(table, tmp_path).rollback("NBA", version, NOW, "Try")


def test_rollback_to_a_version_with_no_calibrators_is_refused(table: Any, tmp_path: Path) -> None:
    folder = _version(tmp_path, "20261105-0000-nocalib")
    for file in (folder / "calibrators").glob("*"):
        file.unlink()
    (folder / "calibrators").rmdir()
    with pytest.raises(RegistryError, match="calibrators"):
        _registry(table, tmp_path).rollback("NBA", "20261105-0000-nocalib", NOW, "Try")


def test_the_pointer_survives_the_local_database_forgetting(table: Any, tmp_path: Path) -> None:
    _version(tmp_path, "20261106-1000-aaaaaaa")
    registry = _registry(table, tmp_path)
    first = registry.bootstrap("NBA", NOW)
    table.delete_item(Key={"pk": "CHAMPION#NBA", "sk": "CURRENT"})
    assert registry.require("NBA") == first
    assert table.get_item(Key={"pk": "CHAMPION#NBA", "sk": "CURRENT"}).get("Item")


def test_new_calibrators_leave_other_families_alone(table: Any, tmp_path: Path) -> None:
    _version(tmp_path, "20261106-1000-aaaaaaa")
    registry = _registry(table, tmp_path)
    first = registry.bootstrap("NBA", NOW)
    better = Calibrator("platt", (0.5,), (0.1,), 500)
    updated = registry.adopt_calibrators(
        "NBA", first, {"total_1h": better}, {"total_1h": "real"}, NOW, {}
    )
    loaded = registry.calibrators("NBA", updated)
    assert loaded["total_1h"] == better
    assert loaded["handicap"] == IDENTITY
    assert updated.version == first.version
    assert updated.calibrator_sources["total_1h"] == "real"
    assert updated.calibrator_sources["team_total"] == "synthetic"


def test_real_calibration_ends_a_markets_unproven_label() -> None:
    markets = config().markets
    markets["NBA"]["total_1h"] = markets["NBA"]["total_1h"].model_copy(
        update={"edge_label": "unproven"}
    )
    markets["NBA"]["team_total"] = markets["NBA"]["team_total"].model_copy(
        update={"edge_label": "unproven"}
    )
    updated = with_calibrator_sources(
        config(markets=markets), "NBA", {"total_1h": "real", "team_total": "synthetic"}
    )
    assert updated.markets["NBA"]["total_1h"].edge_label == "measured"
    assert updated.markets["NBA"]["team_total"].edge_label == "unproven"


# Recalibration.


def _rows(count: int, last_day: date, days: int, shift: float, seed: int = 3) -> pl.DataFrame:
    """Chances the model overstates: the true chance sits `shift` nearer to 50%."""
    rng = np.random.default_rng(seed)
    p = rng.uniform(0.3, 0.7, count)
    truth = 0.5 + (p - 0.5) * shift
    dates = [last_day - timedelta(days=int(number)) for number in rng.integers(0, days, count)]
    return pl.DataFrame(
        {
            "game_id": [f"g{seed}-{number}" for number in range(count)],
            "slate_date_et": dates,
            "family": ["total_1h"] * count,
            "p": p,
            "outcome": (rng.random(count) < truth).astype(float),
        }
    )


YESTERDAY = TODAY - timedelta(days=1)


def test_a_calibrator_that_beats_the_current_one_is_adopted() -> None:
    overconfident = Calibrator("platt", (1.8,), (0.0,), 1000)
    rows = _rows(1500, YESTERDAY, 50, 0.2)
    result = recalibrate_family("total_1h", rows, overconfident, TODAY)
    assert result.adopted, result.reason
    assert result.calibrator is not None
    assert result.loss_candidate is not None and result.loss_current is not None
    assert result.loss_candidate < result.loss_current
    # A chance of 0.7 is pulled toward what really happens.
    assert result.calibrator.apply(np.array([0.7]))[0] < 0.62


def test_a_candidate_that_does_not_beat_the_current_one_is_refused() -> None:
    current = Calibrator.fit(*_arrays(_rows(5000, YESTERDAY, 50, 1.0, seed=9)))
    rows = _rows(600, YESTERDAY, 50, 1.0)
    result = recalibrate_family("total_1h", rows, current, TODAY)
    assert not result.adopted
    assert "doesn't clearly beat" in result.reason


def _arrays(rows: pl.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    return rows["p"].to_numpy(), rows["outcome"].to_numpy()


def test_too_little_real_data_keeps_the_current_calibrator() -> None:
    assert "No settled" in recalibrate_family("total_1h", pl.DataFrame(), IDENTITY, TODAY).reason
    thin = _rows(MIN_REAL_ROWS_WITHOUT_TOP_UP - 1, YESTERDAY, 50, 0.5)
    result = recalibrate_family("total_1h", thin, IDENTITY, TODAY)
    assert not result.adopted
    assert "needed" in result.reason


def test_too_few_recent_rows_cannot_judge_a_candidate() -> None:
    old = _rows(800, YESTERDAY - timedelta(days=20), 25, 0.5)
    result = recalibrate_family("total_1h", old, IDENTITY, TODAY)
    assert not result.adopted
    assert "last 14 days" in result.reason


def test_rows_older_than_the_window_or_from_today_are_ignored() -> None:
    rows = _rows(1500, YESTERDAY, 50, 0.2)
    extra = _rows(50000, TODAY - timedelta(days=70), 5, 1.0)
    today = _rows(50000, TODAY, 1, 1.0)
    result = recalibrate_family("total_1h", pl.concat([rows, extra, today]), IDENTITY, TODAY)
    assert result.rows <= 1500


def test_a_top_up_helps_only_when_real_rows_are_thin() -> None:
    thin = _rows(150, YESTERDAY, 30, 0.2)
    top_up = _rows(3000, YESTERDAY, 30, 0.2, seed=8).select("p", "outcome")
    with_top_up = recalibrate_family("handicap", thin, IDENTITY, TODAY, top_up)
    assert with_top_up.adopted, with_top_up.reason
    assert with_top_up.calibrator is not None
    assert with_top_up.calibrator.samples > 3000


def test_half_time_and_team_totals_get_no_synthetic_top_up() -> None:
    # D72: those are the very rows found unreliable, so they wait for real lines.
    thin = pl.concat(
        [
            _rows(50, YESTERDAY, 30, 0.2).with_columns(pl.lit("total_1h").alias("family")),
            _rows(50, YESTERDAY, 30, 0.2).with_columns(pl.lit("handicap").alias("family")),
        ]
    )
    top_up = _rows(3000, YESTERDAY, 30, 0.2, seed=8).select("p", "outcome")
    results = recalibrate(
        thin,
        {name: IDENTITY for name in ("handicap", "total_ft", "total_1h", "team_total")},
        TODAY,
        {"handicap": top_up},
    )
    assert not results["total_1h"].adopted
    assert results["total_1h"].calibrator is None


def test_a_push_never_trains_a_calibrator() -> None:
    rows = _rows(1500, YESTERDAY, 50, 0.2).with_columns(
        pl.when(pl.col("p") > 0.65).then(None).otherwise(pl.col("outcome")).alias("outcome")
    )
    result = recalibrate_family("total_1h", rows, IDENTITY, TODAY)
    assert result.rows == rows["outcome"].drop_nulls().len()


# Compaction.


def _delta(root: Path, table_name: str, day: str, game: str, points: int) -> None:
    folder = root / f"curated/_deltas/{table_name}/{day}"
    folder.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({"game_id": [game], "home_pts": [points]}).write_parquet(
        folder / f"NBA-2027-{game}.parquet"
    )


def test_compaction_folds_only_old_deltas_and_changes_no_results(tmp_path: Path) -> None:
    base = tmp_path / "curated/games/league=NBA/season=2027"
    base.mkdir(parents=True)
    pl.DataFrame({"game_id": ["a", "b"], "home_pts": [1, 2]}).write_parquet(base / "part-0.parquet")
    _delta(tmp_path, "games", "2026-11-01", "b", 20)  # A correction.
    _delta(tmp_path, "games", "2026-11-02", "c", 30)
    _delta(tmp_path, "games", "2026-12-09", "d", 40)  # Recent: live ingest still needs it.
    before = read_table(tmp_path, "games", "NBA", 2027).sort("game_id")
    assert compact_deltas(tmp_path, "NBA", date(2026, 11, 25)) == {"games": 2}
    assert read_table(tmp_path, "games", "NBA", 2027).sort("game_id").equals(before)
    assert (tmp_path / "curated/_deltas/games/2026-12-09/NBA-2027-d.parquet").exists()
    assert not list((tmp_path / "curated/_deltas/games/2026-11-01").glob("*"))
    assert compact_deltas(tmp_path, "NBA", date(2026, 11, 25)) == {}


def test_compaction_leaves_the_other_league_alone(tmp_path: Path) -> None:
    _delta(tmp_path, "games", "2026-11-01", "a", 1)
    folder = tmp_path / "curated/_deltas/games/2026-11-01"
    pl.DataFrame({"game_id": ["x"], "home_pts": [1]}).write_parquet(folder / "NCAA-2027-x.parquet")
    compact_deltas(tmp_path, "NBA", date(2026, 11, 25))
    assert (folder / "NCAA-2027-x.parquet").exists()


# The logs.


def _final(root: Path, game: str, home: int, away: int, slate: date = TODAY) -> None:
    base = root / "curated/games/league=NCAA/season=2027"
    file = base / f"part-{game}.parquet"
    base.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(
        {
            "game_id": [game],
            "home_id": ["h"],
            "away_id": ["a"],
            "neutral": [False],
            "home_pts": [home],
            "away_pts": [away],
            "home_1h": [home // 2],
            "away_1h": [away // 2],
            "slate_date_et": [slate],
            "season": [2027],
            "tip_utc": [datetime(slate.year, slate.month, slate.day, 23, tzinfo=UTC)],
            "postseason": [False],
        }
    ).write_parquet(file)


def test_every_offered_main_line_is_logged_not_only_the_picks(tmp_path: Path) -> None:
    predictions = [prediction("g1", ft=period(100.0, 100.0))]
    offers = [offer("g1", line=200.5), offer("g1", line=210.5, odds=(1.4, 2.8))]
    scored = score_offers(predictions, offers)
    assert [item.line for item in scored] == [200.5]  # The most even, whatever the model thinks.
    assert scored[0].fair_above == pytest.approx(0.5)
    assert scored[0].p_above == pytest.approx(0.5, abs=0.05)
    write_log(tmp_path, "NCAA", TODAY, "run1", predictions, scored)
    assert has_log(tmp_path, "NCAA", TODAY, "run1")


def test_lines_with_no_match_prediction_or_bookable_price_are_not_logged() -> None:
    predictions = [prediction("g1", h1=None)]
    assert score_offers(predictions, [offer(None)]) == []
    assert score_offers(predictions, [offer("other")]) == []
    assert score_offers(predictions, [offer("g1", bookable=False)]) == []
    assert score_offers(predictions, [offer("g1", "total_1h", line=100.5)]) == []


def test_scored_rows_settle_each_line_and_keep_the_last_seen(tmp_path: Path) -> None:
    _final(tmp_path, "g1", 110, 100)
    _final(tmp_path, "g2", 105, 95)
    predictions = [prediction("g1"), prediction("g2")]
    early = TODAY_TIME - timedelta(hours=2)
    first = score_offers(predictions, [offer("g1", line=205.5, fetched_at=early)])
    later = score_offers(
        predictions,
        [
            offer("g1", line=215.5, fetched_at=TODAY_TIME),
            offer("g2", line=200.0, fetched_at=TODAY_TIME),
        ],
    )
    write_log(tmp_path, "NCAA", TODAY, "a", predictions, first)
    write_log(tmp_path, "NCAA", TODAY, "b", predictions, later)
    rows = scored_rows(tmp_path, "NCAA", TODAY).sort("game_id")
    assert rows["game_id"].to_list() == ["g1", "g2"]
    assert rows["line"].to_list() == [215.5, 200.0]  # g1's later line.
    assert rows["outcome"].to_list() == [0.0, None]  # 210 is under 215.5; g2 pushes on 200.


TODAY_TIME = datetime(2026, 12, 10, 18, 0, tzinfo=UTC)


def test_rows_with_no_final_are_left_out(tmp_path: Path) -> None:
    _final(tmp_path, "other", 1, 1)
    predictions = [prediction("g1")]
    write_log(tmp_path, "NCAA", TODAY, "a", predictions, score_offers(predictions, [offer("g1")]))
    assert not len(scored_rows(tmp_path, "NCAA", TODAY))
    assert not len(prediction_rows(tmp_path, "NCAA", TODAY))


def test_no_log_gives_empty_rows(tmp_path: Path) -> None:
    assert not len(scored_rows(tmp_path, "NCAA", TODAY))
    assert not len(prediction_rows(tmp_path, "NCAA", TODAY))


# Collected lines.


def _snapshot(
    root: Path, day: str, stamp: str, event: dict[str, Any], complete: bool = True
) -> None:
    folder = root / f"raw/sportybet/collector-{day}"
    folder.mkdir(parents=True, exist_ok=True)
    document = {
        "captured_at": stamp,
        "complete": complete,
        "responses": [{"data": {"tournaments": [{"events": [event]}]}}],
    }
    name = f"offers-NCAA-{stamp.replace(':', '')}.json.gz"
    with gzip.open(folder / name, "wt", encoding="utf-8") as handle:
        json.dump(document, handle)


def _recorded_event() -> dict[str, Any]:
    path = Path(__file__).parent / "fixtures/sportybet/events-ncaa-page1.json"
    event: dict[str, Any] = json.loads(path.read_text("utf-8"))["data"]["tournaments"][0]["events"][
        0
    ]
    return event


def test_the_last_line_before_tip_is_used_and_never_one_after(tmp_path: Path) -> None:
    event = _recorded_event()
    tip = datetime.fromtimestamp(event["estimateStartTime"] / 1000, UTC)
    slate = date(2026, 11, 5)
    day = f"{tip:%Y%m%d}"
    before = (tip - timedelta(hours=1)).isoformat()
    last = (tip - timedelta(minutes=5)).isoformat()
    after = (tip + timedelta(minutes=5)).isoformat()
    _snapshot(tmp_path, day, before, event)
    _snapshot(tmp_path, day, last, event)
    _snapshot(tmp_path, day, after, event)
    lines = last_lines(tmp_path, "NCAA", slate)
    assert list(lines) == [event["eventId"]]
    assert lines[event["eventId"]][0] == tip - timedelta(minutes=5)


def test_an_incomplete_snapshot_or_a_bad_event_is_skipped(tmp_path: Path) -> None:
    event = _recorded_event()
    tip = datetime.fromtimestamp(event["estimateStartTime"] / 1000, UTC)
    day = f"{tip:%Y%m%d}"
    _snapshot(tmp_path, day, (tip - timedelta(hours=1)).isoformat(), event, complete=False)
    bad = {**event, "eventId": "bad", "markets": [{"id": "225", "outcomes": []}]}
    _snapshot(tmp_path, day, (tip - timedelta(hours=2)).isoformat(), bad)
    assert last_lines(tmp_path, "NCAA", date(2026, 11, 5)) == {}


def test_only_finished_slates_after_the_cutoff_are_scored() -> None:
    cutoff = datetime(2026, 11, 6, 20, 0, tzinfo=UTC)
    now = datetime(2026, 11, 8, 15, 0, tzinfo=UTC)
    assert slates_to_score(cutoff, now) == [date(2026, 11, 6), date(2026, 11, 7)]
    assert slates_to_score(cutoff, cutoff) == []


def test_a_slate_already_scored_or_with_nothing_new_is_skipped(tmp_path: Path) -> None:
    _final(tmp_path, "g1", 100, 90, date(2026, 11, 7))
    index: Any = None
    late = datetime(2027, 1, 1, tzinfo=UTC)
    run = datetime(2026, 11, 7, 21, tzinfo=UTC)
    result = collected_module.score_slate(
        tmp_path, "NCAA", date(2026, 11, 7), late, tmp_path, run, index, TODAY
    )
    assert "cutoff" in result["skipped"]
    early = datetime(2026, 11, 1, tzinfo=UTC)
    result = collected_module.score_slate(
        tmp_path, "NCAA", date(2026, 11, 7), early, tmp_path, run, index, TODAY
    )
    assert "no saved lines" in result["skipped"]
    predictions = [prediction("g1")]
    write_log(
        tmp_path, "NCAA", date(2026, 11, 7), collected_module.COLLECTED, predictions,
        score_offers(predictions, [offer("g1")]),
    )  # fmt: skip
    result = collected_module.score_slate(
        tmp_path, "NCAA", date(2026, 11, 7), early, tmp_path, run, index, TODAY
    )
    assert result["skipped"] == "already scored"


# Monitoring.


def _logged(count: int, last_day: date, bias: float, sd_scale: float = 1.0) -> pl.DataFrame:
    rng = np.random.default_rng(5)
    mean_home, mean_away = np.full(count, 110.0), np.full(count, 105.0)
    home = np.rint(mean_home + bias + rng.normal(0, 8 * sd_scale, count)).astype(int)
    away = np.rint(mean_away + rng.normal(0, 8 * sd_scale, count)).astype(int)
    return pl.DataFrame(
        {
            "game_id": [f"g{i}" for i in range(count)],
            "slate_date_et": [last_day] * count,
            "home_id": ["h"] * count, "away_id": ["a"] * count, "neutral": [False] * count,
            "home_pts": home, "away_pts": away,
            "home_1h": home // 2, "away_1h": away // 2,
            "ft_mu_home": mean_home, "ft_mu_away": mean_away,
            "ft_sd_margin": np.full(count, 11.3), "ft_sd_total": np.full(count, 11.3),
            "h1_mu_home": mean_home / 2, "h1_mu_away": mean_away / 2,
            "h1_sd_margin": np.full(count, 5.66), "h1_sd_total": np.full(count, 5.66),
            "rest_h": [1.0, 3.0] * (count // 2), "games_h": [5.0, 20.0] * (count // 2),
        }
    )  # fmt: skip


def test_a_healthy_week_raises_no_flags() -> None:
    report = drift(_logged(200, YESTERDAY, 0.0), TODAY)
    assert report["flags"] == []
    assert abs(report["targets"]["ft_margin"]["bias"]) < 2


def test_a_biased_week_is_flagged() -> None:
    report = drift(_logged(200, YESTERDAY, 5.0), TODAY)
    assert any("ft_margin bias" in flag for flag in report["flags"])


def test_a_wrong_spread_is_flagged() -> None:
    report = drift(_logged(200, YESTERDAY, 0.0, sd_scale=1.5), TODAY)
    assert any("spread ratio" in flag for flag in report["flags"])


def test_too_few_games_or_old_games_are_not_judged() -> None:
    assert drift(_logged(10, YESTERDAY, 9.0), TODAY)["flags"] == []
    assert drift(_logged(200, TODAY - timedelta(days=30), 9.0), TODAY)["games"] == 0
    assert drift(pl.DataFrame(), TODAY)["games"] == 0


def test_the_error_report_covers_every_cut() -> None:
    scored = pl.DataFrame(
        {"family": ["total_1h"] * 3, "p": [0.7, 0.3, 0.6], "outcome": [1.0, 1.0, None]}
    )
    report = error_report(_logged(20, YESTERDAY, 1.0), scored)
    for name in (
        "by_line_size",
        "by_favourite",
        "by_venue",
        "by_rest",
        "by_season_stage",
        "by_team",
    ):
        assert report[name], name
    # 0.7 hit, and 0.3 (so 0.7 the other way) missed. The push settles nothing.
    assert report["hit_rate"] == [
        {"family": "total_1h", "bucket": 0.7, "legs": 2, "predicted": 0.7, "hit_rate": 0.5}
    ]


def test_the_error_report_survives_no_data() -> None:
    assert error_report(pl.DataFrame(), pl.DataFrame()) == {"games": 0, "hit_rate": []}


# Champion against challenger, with real small models.


def _frame() -> pl.DataFrame:
    return feature_frame(games_per_day=8, days=100, start=date(2026, 9, 1), season=2027)


def _champion_version(root: Path, frame: pl.DataFrame, name: str, league: str = "NBA") -> Path:
    folder = save_bundle(fit_bundle(frame, FAST), root, league, name, frame)
    (folder / "calibrators").mkdir()
    for family in ("handicap", "total_ft", "total_1h", "team_total"):
        (folder / "calibrators" / f"{family}.json").write_text(
            json.dumps(IDENTITY.to_json()), "utf-8"
        )
    return folder


def _learning(table: Any, root: Path, sha: str | None = SHA) -> Learning:
    return Learning(root, PipelineStore(table), ModelRegistry(table, root), NOW, sha)


def _champion(version: str, cutoff: datetime) -> Champion:
    return Champion(version, version, NOW.isoformat(), "start", cutoff.isoformat())


def test_holdout_metrics_favour_the_model_with_the_signal() -> None:
    frame = _frame()
    train = frame.filter(pl.col("slate_date_et") < date(2026, 11, 20))
    holdout = frame.filter(pl.col("slate_date_et") >= date(2026, 11, 20))
    good = fit_bundle(train, FAST)
    weak = fit_bundle(train.head(120), FAST)
    metrics = holdout_metrics(train, holdout, "NBA", weak, good)
    assert metrics.games == len(holdout)
    assert metrics.log_likelihood.mean > 0
    assert set(metrics.brier_changes) == {"handicap", "total_ft", "total_1h", "team_total"}
    assert all(set(pair) == {"champion", "challenger"} for pair in metrics.brier_scores.values())


def test_a_champion_that_saw_the_holdout_is_skipped_and_recorded(
    table: Any, tmp_path: Path
) -> None:
    ctx = _learning(table, tmp_path)
    champion = _champion("20261209-1000-aaaaaaa", NOW - timedelta(days=1))
    result = step_challenger(ctx, "NBA", champion, _frame(), FAST)
    assert result["decision"] == "skipped"
    assert "D16" in result["reason"]
    assert ctx.registry.history("NBA")[0]["decision"] == "skipped"


def test_a_short_holdout_is_rejected_without_training_anything(table: Any, tmp_path: Path) -> None:
    ctx = _learning(table, tmp_path)
    champion = _champion("20261001-1000-aaaaaaa", datetime(2026, 10, 1, tzinfo=UTC))
    thin = _frame().filter(pl.col("slate_date_et") < date(2026, 11, 13))
    result = step_challenger(ctx, "NBA", champion, thin, FAST)
    assert result["decision"] == "rejected"
    assert "needed" in result["reason"]


def _promotable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "hoopsedge.learning.run.decide",
        lambda *_: SimpleNamespace(promote=True, reasons=("Clearly better",)),
    )


def test_a_winning_challenger_is_refit_saved_and_promoted(
    table: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    frame = _frame()
    version = "20261001-1000-aaaaaaa"
    early = frame.filter(pl.col("slate_date_et") < date(2026, 10, 1))
    _champion_version(tmp_path, early, version)
    ctx = _learning(table, tmp_path)
    champion = ctx.registry.bootstrap("NBA", datetime(2026, 10, 1, tzinfo=UTC))
    _promotable(monkeypatch)
    result = step_challenger(ctx, "NBA", champion, frame, FAST)
    assert result["decision"] == "promoted"
    current = ctx.registry.require("NBA")
    assert current.version == result["version"]
    assert current.version.endswith("abc1234")
    folder = ctx.registry.model_folder("NBA", current.version)
    assert (folder / "metrics.json").exists()
    assert (folder / "evidence.json").exists()
    assert set(ctx.registry.calibrators("NBA", current)) == {
        "handicap", "total_ft", "total_1h", "team_total",
    }  # fmt: skip
    # Rollback demonstrated on real artifacts.
    ctx.registry.rollback("NBA", version, NOW, "Testing")
    assert ctx.registry.require("NBA").version == version


def test_without_a_commit_id_a_winner_cannot_be_promoted(
    table: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    frame = _frame()
    version = "20261001-1000-aaaaaaa"
    _champion_version(tmp_path, frame.filter(pl.col("slate_date_et") < date(2026, 10, 1)), version)
    ctx = _learning(table, tmp_path, sha=None)
    champion = ctx.registry.bootstrap("NBA", NOW)
    _promotable(monkeypatch)
    result = step_challenger(ctx, "NBA", champion, frame, FAST)
    assert result["decision"] == "rejected"
    assert ctx.registry.require("NBA").version == version


def test_a_challenger_that_loses_changes_nothing_but_is_recorded(
    table: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    frame = _frame()
    version = "20261001-1000-aaaaaaa"
    _champion_version(tmp_path, frame.filter(pl.col("slate_date_et") < date(2026, 10, 1)), version)
    ctx = _learning(table, tmp_path)
    champion = ctx.registry.bootstrap("NBA", NOW)
    monkeypatch.setattr(
        "hoopsedge.learning.run.decide",
        lambda *_: SimpleNamespace(promote=False, reasons=("Not better",)),
    )
    result = step_challenger(ctx, "NBA", champion, frame, FAST)
    assert result["decision"] == "rejected"
    assert result["metrics"]["games"] > 0
    assert ctx.registry.require("NBA").version == version
    assert ctx.registry.history("NBA")[-1]["reasons"] == ["Not better"]


# A job nobody watches has to fail loudly.


def test_a_failed_learning_run_alerts_and_still_writes_its_report(
    table: Any, tmp_path: Path
) -> None:
    ctx = _learning(table, tmp_path)
    with pytest.raises(RegistryError):
        learn(ctx, "NBA")  # No champion yet.
    alerts = table.query(KeyConditionExpression=Key("pk").eq("ALERT"))["Items"]
    assert [alert["severity"] for alert in alerts] == ["CRITICAL"]
    report = json.loads((tmp_path / "reports/learning/NBA/2026-12-10.json").read_text("utf-8"))
    assert report["status"] == "failed"
    assert "RegistryError" in report["error"]


def test_learning_flags_drift_and_writes_a_full_report(
    table: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _version(tmp_path, "20261106-1000-aaaaaaa")
    ctx = _learning(table, tmp_path)
    ctx.registry.bootstrap("NBA", NOW)
    module = "hoopsedge.learning.run"
    monkeypatch.setattr(f"{module}.compact_deltas", lambda *_: {})
    monkeypatch.setattr(f"{module}.build_training_set", lambda *_: {})
    monkeypatch.setattr(f"{module}.step_collected", lambda *_: [])
    monkeypatch.setattr(f"{module}.load_frame", lambda *_: _logged(40, YESTERDAY, 9.0))
    monkeypatch.setattr(f"{module}.load_params", lambda _: FAST)
    monkeypatch.setattr(f"{module}.step_challenger", lambda *_: {"decision": "skipped"})
    monkeypatch.setattr(f"{module}.prediction_rows", lambda *_: _logged(40, YESTERDAY, 9.0))
    monkeypatch.setattr(f"{module}.scored_rows", lambda *_: pl.DataFrame())
    report = learn(ctx, "NBA")
    assert report["status"] == "succeeded"
    assert report["challenger"] == {"decision": "skipped"}
    assert any("bias" in flag for flag in report["drift"]["flags"])
    alerts = table.query(KeyConditionExpression=Key("pk").eq("ALERT"))["Items"]
    assert any(alert["severity"] == "WARNING" for alert in alerts)
    assert report["champion"]["version"] == "20261106-1000-aaaaaaa"
    assert (tmp_path / "reports/learning/NBA/2026-12-10.json").exists()
    assert set(report["recalibration"]) == {"handicap", "total_ft", "total_1h", "team_total"}


def test_a_new_real_calibrator_warns_to_review_thresholds(
    table: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _version(tmp_path, "20261106-1000-aaaaaaa")
    ctx = _learning(table, tmp_path)
    champion = ctx.registry.bootstrap("NBA", NOW)
    module = "hoopsedge.learning.run"
    overconfident = Calibrator("platt", (1.0,), (0.0,), 1000)
    monkeypatch.setattr(f"{module}.scored_rows", lambda *_: _rows(1500, YESTERDAY, 50, 0.1))
    monkeypatch.setattr(f"{module}._top_ups", lambda *_: {})
    monkeypatch.setattr(
        ctx.registry, "calibrators", lambda *_: dict.fromkeys(FAMILY_NAMES, overconfident)
    )
    updated, results = step_recalibrate(ctx, "NBA", champion)
    assert results["total_1h"].adopted, results["total_1h"].reason
    assert updated.calibrator_sources["total_1h"] == "real"
    alerts = table.query(KeyConditionExpression=Key("pk").eq("ALERT"))["Items"]
    assert any("review its thresholds" in alert["message"] for alert in alerts)


def test_finals_load_with_their_first_half_scores(tmp_path: Path) -> None:
    _final(tmp_path, "g1", 100, 90)
    finals = load_finals(tmp_path, "NCAA", TODAY)
    assert finals["g1"] == Final(100, 90, 50, 45)
    assert load_finals(tmp_path, "NBA", TODAY) == {}


# The daily cycle.


LAGOS_11 = datetime(2026, 11, 7, 10, 0, tzinfo=UTC)  # 11:00 in Lagos.


def _names(now: datetime, done: dict[str, str] | None = None) -> set[str]:
    return {job.name for job in due_jobs(now, done or {"grade": now.isoformat()})}


def test_learning_starts_at_eleven_lagos_one_league_at_a_time() -> None:
    assert _names(LAGOS_11) == {"learn-NBA", "learn-NCAA"}
    assert _names(LAGOS_11 - timedelta(minutes=1)) == set()


def test_each_slate_runs_at_its_own_time() -> None:
    assert _names(datetime(2026, 11, 7, 14, 0, tzinfo=UTC)) == {"slate-NCAA"}
    assert _names(datetime(2026, 11, 7, 21, 0, tzinfo=UTC)) == {"slate-NBA"}  # D71: 22:00 Lagos.


def test_the_slate_date_is_the_eastern_date() -> None:
    jobs = due_jobs(
        datetime(2026, 11, 7, 21, 0, tzinfo=UTC), {"grade": "2026-11-07T21:00:00+00:00"}
    )
    assert "2026-11-07" in jobs[0].command


def test_a_job_done_today_is_not_repeated() -> None:
    done = {"grade": LAGOS_11.isoformat(), "learn-NBA": "2026-11-07", "learn-NCAA": "2026-11-07"}
    assert _names(LAGOS_11, done) == set()


def test_a_job_missed_by_hours_waits_for_tomorrow() -> None:
    assert _names(LAGOS_11 + timedelta(hours=2, minutes=59)) == {"learn-NBA", "learn-NCAA"}
    assert _names(LAGOS_11 + timedelta(hours=3)) == set()


def test_grading_runs_every_thirty_minutes() -> None:
    recent = (LAGOS_11 - timedelta(minutes=29)).isoformat()
    finished = {"learn-NBA": "2026-11-07", "learn-NCAA": "2026-11-07"}
    assert _names(LAGOS_11, {"grade": recent, **finished}) == set()
    old = (LAGOS_11 - timedelta(minutes=30)).isoformat()
    assert _names(LAGOS_11, {"grade": old, **finished}) == {"grade"}
    assert "grade" in {job.name for job in due_jobs(LAGOS_11, {})}


def test_a_failed_job_is_logged_and_the_rest_still_run(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def runner(command: list[str], **_: Any) -> SimpleNamespace:
        calls.append(command)
        return SimpleNamespace(returncode=1 if len(calls) == 1 else 0)

    results = run_due(tmp_path, LAGOS_11, runner)
    assert [item["exit"] for item in results] == [1, 0, 0]
    assert len((tmp_path / "runs.jsonl").read_text("utf-8").splitlines()) == 3
    assert run_due(tmp_path, LAGOS_11 + timedelta(seconds=30), runner) == []


def test_a_pause_file_stops_everything(tmp_path: Path) -> None:
    (tmp_path / "PAUSED").write_text("", "utf-8")
    assert run_due(tmp_path, LAGOS_11, lambda *_, **__: None) == []
