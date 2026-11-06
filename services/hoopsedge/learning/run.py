"""The nightly learning job, one league at a time (docs/08 section 7.3, C6).

    docker compose run --rm pipeline python -m hoopsedge.learning.run bootstrap --league NBA
    docker compose run --rm pipeline python -m hoopsedge.learning.run learn --league NBA
    docker compose run --rm pipeline python -m hoopsedge.learning.run rollback --league NBA \
        --version 20261106-1954-4d25ce0 --reason "Why"

Ratings already learn from every game, because each prediction replays results as of that moment.
This job handles what changes more slowly: calibrators, and whether a retrained model has earned
the job. It never silently makes things worse: every decision is written down with its numbers.
"""

import argparse
import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from hoopsedge.backtest.data import load_frame, load_lines, load_predictions
from hoopsedge.backtest.markets import REAL, score_markets
from hoopsedge.backtest.run import load_params
from hoopsedge.backtest.walkforward import known_before
from hoopsedge.common.dynamo import connect, ensure_table, table_name
from hoopsedge.features.training import build_training_set, slate_run_time
from hoopsedge.ingest.backfill import FIRST_SEASON, current_season
from hoopsedge.ingest.curated import compact_deltas
from hoopsedge.ingest.espn_coverage import EASTERN
from hoopsedge.learning.collected import score_slate, slates_to_score
from hoopsedge.learning.data import prediction_rows, scored_rows
from hoopsedge.learning.evaluate import holdout_metrics
from hoopsedge.learning.monitor import drift, error_report
from hoopsedge.learning.promotion import HOLDOUT_DAYS, MIN_HOLDOUT_GAMES, decide, fair_to_compare
from hoopsedge.learning.recalibrate import FamilyResult, recalibrate
from hoopsedge.learning.registry import REAL as REAL_SOURCE
from hoopsedge.learning.registry import Champion, ModelRegistry
from hoopsedge.models.registry import load_bundle, model_version, save_bundle
from hoopsedge.models.training import fit_bundle
from hoopsedge.pipeline.store import CRITICAL, WARNING, PipelineStore
from hoopsedge.teams.matcher import TeamIndex, confirmed_aliases

DEFAULT_DATA_DIR = "/workspace/data"
# Live ingest decides what it has processed by a delta's existence, within its 14-day scan.
COMPACT_AFTER = timedelta(days=15)


@dataclass
class Learning:
    data_dir: Path
    store: PipelineStore
    registry: ModelRegistry
    now: datetime
    git_sha: str | None

    @property
    def today(self) -> date:
        return self.now.astimezone(EASTERN).date()


def _top_ups(ctx: Learning, league: str) -> dict[str, pl.DataFrame]:
    """Last season's walk-forward rows at real closing lines, for the families that have them."""
    try:
        predictions = load_predictions(ctx.data_dir, league).filter(pl.col("model") == "model")
        lines = load_lines(ctx.data_dir, league).filter(pl.col("kind") == "close")
    except (FileNotFoundError, OSError):
        return {}
    last_season = current_season(ctx.today) - 1
    scored = score_markets(predictions.filter(pl.col("season") == last_season), lines).filter(
        (pl.col("source") == REAL) & pl.col("outcome").is_not_null()
    )
    return {
        family: scored.filter(pl.col("family") == family).select("p", "outcome")
        for family in ("handicap", "total_ft")
    }


def step_collected(ctx: Learning, league: str, champion: Champion) -> list[dict[str, Any]]:
    index = TeamIndex.load(
        ctx.data_dir, league, confirmed_aliases(ctx.store.confirmed_aliases(league))
    )
    cutoff = datetime.fromisoformat(champion.cutoff)
    folder = ctx.registry.model_folder(league, champion.version)
    return [
        score_slate(
            ctx.data_dir, league, slate, cutoff, folder, slate_run_time(league, slate), index,
            ctx.today,
        )
        for slate in slates_to_score(cutoff, ctx.now)
    ]  # fmt: skip


def step_recalibrate(
    ctx: Learning, league: str, champion: Champion
) -> tuple[Champion, dict[str, FamilyResult]]:
    results = recalibrate(
        scored_rows(ctx.data_dir, league, ctx.today),
        ctx.registry.calibrators(league, champion),
        ctx.today,
        _top_ups(ctx, league),
    )
    adopted = {
        family: result.calibrator
        for family, result in results.items()
        if result.adopted and result.calibrator is not None
    }
    if not adopted:
        return champion, results
    switched = [f for f in adopted if champion.calibrator_sources.get(f) != REAL_SOURCE]
    updated = ctx.registry.adopt_calibrators(
        league,
        champion,
        adopted,
        dict.fromkeys(adopted, REAL_SOURCE),
        ctx.now,
        {"families": {family: results[family].reason for family in adopted}},
    )
    for family in switched:
        # D72: real-line probabilities sit near 50%, so old thresholds may now block every leg.
        ctx.store.alert(
            ctx.now, WARNING, f"thresholds-{league}-{family}",
            f"{league} {family} now uses real-line calibration: review its thresholds", league,
        )  # fmt: skip
    return updated, results


def step_challenger(
    ctx: Learning, league: str, champion: Champion, frame: pl.DataFrame, params: dict[str, Any]
) -> dict[str, Any]:
    """Docs/08 step 4 and 5. Every outcome, including not comparing, is written to the audit."""
    start = ctx.today - timedelta(days=HOLDOUT_DAYS[league])
    start_time = datetime(start.year, start.month, start.day, tzinfo=EASTERN)
    cutoff = datetime.fromisoformat(champion.cutoff)

    def record(decision: str, **details: Any) -> dict[str, Any]:
        ctx.registry.audit(league, ctx.now, champion.version, decision, details)
        return {"decision": decision, **details}

    if not fair_to_compare(cutoff, start_time):
        return record("skipped", reason=f"Champion trained up to {cutoff}, into the holdout (D16)")
    holdout = frame.filter(
        (pl.col("slate_date_et") >= start) & (pl.col("slate_date_et") < ctx.today)
    )
    minimum = MIN_HOLDOUT_GAMES[league]
    if len(holdout) < minimum:
        return record("rejected", reason=f"Holdout has {len(holdout)} games, {minimum} needed")
    train = known_before(frame, league, start)
    challenger = fit_bundle(train, params)
    current = load_bundle(ctx.registry.model_folder(league, champion.version))
    metrics = holdout_metrics(train, holdout, league, current, challenger)
    verdict = decide(
        league, metrics.games, metrics.log_likelihood, metrics.crps, metrics.brier_changes
    )
    details = {"reasons": list(verdict.reasons), "metrics": metrics.to_json()}
    if not verdict.promote:
        return record("rejected", **details)
    if not ctx.git_sha:
        return record("rejected", **{**details, "reasons": ["No HOOPSEDGE_GIT_SHA: can't promote"]})
    # Earned on the holdout. The artifact itself may now learn from those games too.
    final_rows = known_before(frame, league, ctx.today + timedelta(days=1))
    final = fit_bundle(final_rows, params)
    version = model_version(ctx.now, ctx.git_sha, dirty=False)
    folder = save_bundle(final, ctx.registry.root, league, version, final_rows)
    (folder / "metrics.json").write_text(json.dumps(metrics.to_json(), indent=1), "utf-8")
    (folder / "evidence.json").write_text(json.dumps(details, indent=1), "utf-8")
    ctx.registry.promote(league, version, champion, ctx.now, verdict.reasons[0])
    return {"decision": "promoted", "version": version, **details}


def _with_context(frame: pl.DataFrame, rows: pl.DataFrame) -> pl.DataFrame:
    if not len(rows):
        return rows
    context = frame.select("game_id", "rest_h", "games_h")
    return rows.join(context, on="game_id", how="left")


def learn(ctx: Learning, league: str) -> dict[str, Any]:
    """Runs everything for one league. A failure is recorded and alerted, then raised."""
    started = time.monotonic()
    report: dict[str, Any] = {"league": league, "date": ctx.today.isoformat(), "steps": {}}

    def timed(name: str, seconds_from: float) -> None:
        report["steps"][name] = round(time.monotonic() - seconds_from, 1)

    try:
        champion = ctx.registry.require(league)
        moment = time.monotonic()
        report["compacted"] = compact_deltas(ctx.data_dir, league, ctx.today - COMPACT_AFTER)
        seasons = list(range(FIRST_SEASON[league], current_season(ctx.today) + 1))
        build_training_set(ctx.data_dir, league, seasons, lambda _: None)
        timed("compact_and_features", moment)

        moment = time.monotonic()
        report["collected"] = step_collected(ctx, league, champion)
        timed("score_collected", moment)

        moment = time.monotonic()
        champion, results = step_recalibrate(ctx, league, champion)
        report["recalibration"] = {
            family: {
                "adopted": result.adopted,
                "reason": result.reason,
                "rows": result.rows,
                "loss_current": result.loss_current,
                "loss_candidate": result.loss_candidate,
            }
            for family, result in results.items()
        }
        timed("recalibrate", moment)

        moment = time.monotonic()
        frame = load_frame(ctx.data_dir, league)
        report["challenger"] = step_challenger(ctx, league, champion, frame, load_params(league))
        timed("challenger", moment)

        moment = time.monotonic()
        logged = _with_context(frame, prediction_rows(ctx.data_dir, league, ctx.today))
        report["drift"] = drift(logged, ctx.today)
        for flag in report["drift"]["flags"]:
            ctx.store.alert(ctx.now, WARNING, f"drift-{league}", f"{league}: {flag}", league)
        report["errors"] = error_report(logged, scored_rows(ctx.data_dir, league, ctx.today))
        timed("monitor", moment)
        report["champion"] = asdict(ctx.registry.require(league))
        report["seconds"] = round(time.monotonic() - started, 1)
        report["status"] = "succeeded"
    except Exception as error:
        report["status"] = "failed"
        report["error"] = f"{type(error).__name__}: {error}"
        ctx.store.alert(ctx.now, CRITICAL, f"learn-{league}", report["error"], league)
        raise
    finally:
        folder = ctx.data_dir / "reports" / "learning" / league
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{ctx.today}.json").write_text(
            json.dumps(report, indent=1, default=str), "utf-8"
        )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["bootstrap", "learn", "rollback", "status"])
    parser.add_argument("--league", choices=["NBA", "NCAA"], required=True)
    parser.add_argument("--version")
    parser.add_argument("--reason", default="Owner's rollback")
    args = parser.parse_args()
    data_dir = Path(os.environ.get("HOOPSEDGE_DATA_DIR", DEFAULT_DATA_DIR))
    resource = connect()
    ensure_table(resource, table_name())
    table = resource.Table(table_name())
    registry = ModelRegistry(table, data_dir / "models")
    now = datetime.now(UTC)
    ctx = Learning(
        data_dir, PipelineStore(table), registry, now, os.environ.get("HOOPSEDGE_GIT_SHA") or None
    )
    if args.command == "bootstrap":
        print(asdict(registry.bootstrap(args.league, now)))
    elif args.command == "rollback":
        if not args.version:
            parser.error("rollback needs --version")
        print(asdict(registry.rollback(args.league, args.version, now, args.reason)))
    elif args.command == "status":
        print(json.dumps(asdict(registry.require(args.league)), indent=1))
        print(json.dumps(registry.history(args.league)[-5:], indent=1, default=str))
    else:
        result = learn(ctx, args.league)
        summary = {key: result[key] for key in ("status", "seconds", "steps", "challenger")}
        print(
            json.dumps({**summary, "recalibration": result["recalibration"]}, indent=1, default=str)
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
