# HoopsEdge

Private basketball prediction system for the NBA and NCAA Men's D1. Start with `CLAUDE.md` and `docs/BUILD-PLAN.md`.

## Local development

You only need Docker. Run these from the repo root:

```sh
docker compose build dev                    # build the toolchain
docker compose run --rm dev pytest          # tests
docker compose run --rm dev ruff check .    # lint
docker compose run --rm dev ruff format --check .
docker compose run --rm dev mypy            # type checks
docker compose run --rm dev bash            # shell
```

If you change dependencies in `services/pyproject.toml`, refresh the lock file and rebuild:

```sh
docker compose run --rm dev uv lock
docker compose build dev
```

## Early data collector

Snapshots SportyBet offers, NBA injuries and ESPN lines to `data/raw/` (D13). It only collects while this machine and Docker are running, and gaps can't be recovered.

```sh
docker compose up -d --build collector                                       # start, or pick up code changes
docker compose run --rm dev python -m hoopsedge.ingest.collector status      # health and listing growth
docker compose logs --tail 20 collector                                      # recent runs
docker compose stop collector                                                # stop
```

If `status` shows `HALTED`, a source returned a block and the collector has stopped calling it. Review what happened before running `docker compose run --rm dev python -m hoopsedge.ingest.collector resume <source>`.

## Historical backfill

A one-time download of past games from ESPN, then odds, into `data/raw/`: NBA from 2018-19 (D39) and NCAA from 2023-24 (D66). It's safe to stop and start again, and it doesn't restart by itself after a reboot. Starting it again resumes where it stopped.

```sh
docker compose up -d backfill                                                # start or resume
docker compose run --rm dev python -m hoopsedge.ingest.backfill status       # progress
docker compose stop backfill                                                 # stop
docker compose run --rm dev python -m hoopsedge.ingest.backfill build        # build tables and docs/reports/phase2-coverage.md
```

`build` can run at any time and rebuilds all curated tables from the raw files. If `status` shows `HALTED`, ESPN returned a block, so review it before running `resume` in place of `status`.

## Live ingest

Every 30 minutes it stores new finals, schedule changes and NBA injuries. It writes one small file per game under `data/curated/_deltas/`, and `read_table` in `hoopsedge/ingest/curated.py` merges those with the season's table. It runs beside `aws-mock`, a local stand-in for DynamoDB that forgets everything on restart, which is safe.

```sh
docker compose up -d --build live-ingest                                     # start, or pick up code changes
docker compose run --rm dev python -m hoopsedge.ingest.live status           # health and the 48-hour count
docker compose run --rm live-ingest python -m hoopsedge.ingest.live once --dates 2026-11-05   # re-run given dates
docker compose stop live-ingest aws-mock                                     # stop
```

It shares its `HALTED` state with the backfill. Review a block before clearing it with `clear-halt` in place of `status`.

## Ratings and training sets

These read the curated tables, so run `backfill build` first. `build` only adds games it hasn't built, and `sanity` writes `docs/reports/phase3-ratings-sanity.md`.

```sh
docker compose run --rm dev python -m hoopsedge.features.training build                 # both leagues
docker compose run --rm dev python -m hoopsedge.features.training sanity                # compare ratings with win %
docker compose run --rm dev python -m hoopsedge.features.training leakage --league NBA  # 500 real games
```

All curated reads go through `hoopsedge/ratings/history.py`. A result counts only once `tip + 3 h < as_of` (D14).

## Models and evidence

Run these in order, per league. The tuned settings, thresholds and market gates are written to `services/hoopsedge/models/params/`, and reports to `docs/reports/`. Model versions go in `data/models/`.

```sh
docker compose run --rm dev python -m hoopsedge.backtest.run lines                      # historical lines, both leagues
docker compose run --rm dev python -m hoopsedge.backtest.run ratings-grid               # docs/reports/phase4-ratings-grid.md
docker compose run --rm dev python -m hoopsedge.backtest.run tune --league NBA          # about 20 min; run once
docker compose run --rm dev python -m hoopsedge.backtest.run backtest --league NBA      # about 15 min; NCAA about 3
docker compose run --rm dev python -m hoopsedge.backtest.run evidence --league NBA      # report, gates and a model version
```

Set `HOOPSEDGE_GIT_SHA` on `evidence` to the commit being run. Without it the version is marked `-dirty` and can't be promoted. After changing `ratings/config.yaml`, delete that league's `data/curated/features` and `ratings_snapshots` folders and rebuild before tuning.

## Ticket engine

`hoopsedge.engine.build_tickets(predictions, offers, config, now)` turns predictions and SportyBet offers into tickets. It's pure: no network, no clock and no randomness, so the same inputs always give the same tickets. Every outcome that doesn't become a leg gets a reason code. Nothing runs it on a schedule until Phase 6 supplies live offers.

`load_engine_config(calibrators)` reads market gates and thresholds from `models/params/`, and `models.registry.load_calibrators(folder)` reads a model version's calibrators. A market with no threshold makes no legs (`NO_THRESHOLD`), and each ticket records whether its market passed its gate.

After a deliberate change to engine output, regenerate the golden file and review its diff:

```sh
docker compose run --rm -e UPDATE_GOLDEN=1 dev pytest -q tests/test_engine.py -k recorded
```

## Slate pipeline

One league and date, end to end: refresh results and injuries, fetch SportyBet offers, match teams, predict, build tickets, re-check offers, then book and verify. It runs against the local database stand-in. **Locally it always runs dry and can't create a real code.**

```sh
docker compose run --rm pipeline python -m hoopsedge.pipeline.slate run --league NCAA --date 2026-11-06
```

It prints the tickets and writes everything behind them, including each rejection's reason, to `data/runs/{run_id}/result.json`. Raw SportyBet responses go to `data/raw/sportybet/run-*/`. Teams it can't place with certainty go to the Unmatched queue and are excluded.

**Half-time and Team O/U probabilities are unproven (D72).** Treat each leg as about 50%, whatever the ticket's figures say: at real bookmaker lines, the model's high-confidence picks won about half the time.

`hoopsedge.pipeline.live_test` made the one approved live booking. All 5 test codes are now used, so it refuses to book again.

## Grading and learning

Grading settles every finished leg, and it's safe to run as often as you like. Learning runs nightly per league: it recalibrates, and tests a retrained model against the champion. A new model only takes over if it's measurably better on games neither trained on, and every decision is recorded with its numbers.

```sh
docker compose run --rm pipeline python -m hoopsedge.grading.run                 # settle finished legs
docker compose run --rm pipeline python -m hoopsedge.grading.run hand-check      # 20 legs to compare with real results
docker compose run --rm pipeline python -m hoopsedge.learning.run bootstrap --league NBA  # first champion
docker compose run --rm pipeline python -m hoopsedge.learning.run learn --league NBA      # the nightly job
docker compose run --rm pipeline python -m hoopsedge.learning.run status --league NBA     # champion and decisions
docker compose run --rm pipeline python -m hoopsedge.learning.run rollback --league NBA --version <version> --reason "Why"
```

Each night's report is at `data/reports/learning/{league}/{date}.json`.

To run the daily cycle by itself: dry-run slates at 15:00 (NCAA) and 22:00 (NBA) Lagos, grading every 30 minutes and learning at 11:00. Set `HOOPSEDGE_GIT_SHA` to the commit you built, or learning refuses to promote a model. Its code is copied into the image, so rebuild to pick up changes. Create `data/scheduler/PAUSED` to pause it, and read `data/scheduler/runs.jsonl` to see what it did.

```sh
docker compose --profile manual up -d --build scheduler
```
