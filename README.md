# HoopsEdge

Private prediction system for the NBA, NCAA men's basketball (NCAAB) and NCAA football (NCAAF). v2 runs on the last-5-games model in `HOOPSEDGE_V2_REBUILD.md`, which overrides `CLAUDE.md` and `docs/` where they conflict. Decisions are in `docs/DECISIONS.md`.

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

## Scores ingest

Final scores, schedules and team levels from ESPN's scoreboard, per sport, into `data/raw/espn/v2/` and the curated `results` and `registry` tables (D93, D96). The first `refresh` of a season is its backfill; after that it only re-reads unsettled dates and the next 7 days. The scheduler runs it at 08:00 Lagos, and every slate run refreshes first.

```sh
docker compose run --rm dev python -m hoopsedge.ingest.scores refresh --sport NCAAB   # NCAAF, NCAAB or NBA
docker compose run --rm dev python -m hoopsedge.ingest.scores status --sport NCAAB
docker compose run --rm dev python -m hoopsedge.ingest.scores resume                  # after a halt
```

If ESPN returns a block, every ESPN job halts until you've reviewed it and run `resume`.

## The L5 model

`hoopsedge/l5/model.py` is pure: last-5 averages, projections, tiers, outlook, lean and team pick, with every threshold from the v2 config (`hoopsedge/common/config_v2_defaults.yaml`). The golden test reproduces the owner's 2026-11-21 NCAAF sheet. To grade a hand-run sheet against ESPN finals:

```sh
docker compose run --rm dev python /workspace/scripts/grade_sheet.py /workspace/services/tests/fixtures/ncaaf_predictions_20261121.csv 2026-11-21
```

## Slates

One sport and US Eastern date: refresh ESPN, list SportyBet's events, match teams (aliases, exact name, location plus mascot; never fuzzy), predict, choose lines, re-check them, and build tickets. Codes are practice `DRYRUN-` codes unless both switches allow real ones (D97): the dashboard's Dry run switch off, and `DRY_RUN=false` on the container. Only the `api` container (Run now) has that locally; scheduled runs follow `HOOPSEDGE_SCHEDULED_DRY_RUN` in `.env`, and command-line runs in `pipeline` always stay dry. A real code is read back and checked selection by selection; a mismatch marks it invalid, alerts, and stops the run's other real bookings. It needs the local database, so it runs in the `pipeline` service:

```sh
docker compose --profile manual run --rm pipeline python -m hoopsedge.v2.slate run --sport NCAAB --date 2026-11-24
docker compose --profile manual run --rm pipeline python -m hoopsedge.v2.slate alias --sport NCAAB --name "SportyBet Name" --team-id 2010
```

To rebook a ticket after some of its games have started (drops those legs, re-checks the rest, books a new code, links the two):

```sh
docker compose --profile manual run --rm pipeline python -m hoopsedge.v2.slate rebook --sport NCAAB --date 2026-11-24 --ticket-id <id>
```

Each run writes `data/v2/runs/{sport}/{date}/{run_id}/`: the CSV in the owner's sheet format, `run.json` with every row, leg, ticket, drop and failure, and the raw SportyBet responses. Unmatched teams go to the review queue in DynamoDB with suggestions; add an alias, then re-run. The slate's `index.json` records which tickets a later run's real codes superseded and which were rebooked.

## Grading and calibration

Every finished game is graded from the last run that started before its kickoff, for both `l5_same_level_only` settings: totals at the main and the `safest` line, team totals at the main line, whether or not the game cleared a threshold. Every ticket leg is graded on its booked line, and each ticket gets a result (D99). Grades are files in `data/v2/grades/{sport}/{date}.json`, re-graded daily until nothing is pending. The dashboard's Results page shows hit rate against the bookmaker-implied rate and flat-stake ROI, by line mode, edge bucket and cross-level, and threshold suggestions once 100 graded legs clear them (D90). A suggestion changes nothing until you apply it, which saves a new config version.

```sh
docker compose run --rm dev python -m hoopsedge.v2.grading grade                       # every sport, finished slates
docker compose run --rm dev python -m hoopsedge.v2.grading grade --sport NCAAB --date 2026-11-24
docker compose --profile manual run --rm pipeline python -m hoopsedge.v2.grading report --sport NCAAB
```

The weekly report is `docs/reports/v2-evidence-{sport}-{week ending}.md`, with a `.json` beside it.

## Scheduler

Lagos times (brief §12): database export 05:00, ESPN refresh 08:00, grading 09:00, the weekly evidence report Mondays 09:30, NCAAB and NBA slates 16:00 and 21:00, NCAAF 18:00 daily and 14:00 on Saturdays. Create `data/scheduler/PAUSED` to pause it, and read `data/scheduler/runs.jsonl` to see what it did. Its code is copied into the image, so rebuild to pick up changes.

```sh
docker compose --profile manual up -d --build --no-deps scheduler
```

## Legacy (v1)

The v1 model (ratings, features, LightGBM models, backtests, learning, the probability engine, the v1 slate, offers, grading and basketball ingest) is in `services/legacy/`, with its tests (D96). Nothing active imports or runs it, and a test enforces that. It isn't deleted: that needs the owner's word.

## API

The dashboard's API. Every request except `/health` needs the shared secret in `x-hoopsedge-key`. Set `HOOPSEDGE_API_KEY` in your shell or an uncommitted `.env` file: with none set, it refuses everything. It listens only on this machine.

```sh
docker compose --profile manual up -d --build --no-deps api
curl -H "x-hoopsedge-key: $HOOPSEDGE_API_KEY" http://127.0.0.1:8000/slates/2026-11-06
```

Changes (`POST`, `PUT` and `DELETE`) also need a unique `idempotency-key` header, and are limited to 10 a minute. The config document is one versioned document: thresholds, line modes, ticket rules, schedules and the Dry run switch, and runs use the version in force when they start (D98).

The endpoints are in `docs/api/openapi.json`. After changing any endpoint, regenerate it, or a test fails:

```sh
docker compose run --rm dev python -m hoopsedge.api.main export
```

## Dashboard

The phone-first dashboard, in `web/`. To run it locally with the API, put `HOOPSEDGE_API_KEY=<any long random value>` in a root `.env` file, which git ignores, then open http://127.0.0.1:3000. It builds on start, which takes about 2 minutes.

```sh
docker compose --profile manual up -d --build --no-deps api web
```

Pages: Today (sport tabs, tickets with codes to copy and rebook, the slate sheet, CSV download), Teams (review queue and aliases), Runs, Config (every threshold, versioned) and System (relay health, alerts). **Run now books real codes only when Config's Dry run is off (D97).** Nothing here can log in or place a bet.

The key stays on the server: the browser only ever talks to the dashboard. Checks, all inside Docker so they don't depend on this machine's Node:

```sh
docker compose --profile manual run --rm --no-deps web npm run typecheck
docker compose --profile manual run --rm --no-deps web npm run lint
docker compose --profile manual run --rm --no-deps web npm test
docker compose --profile manual run --rm --no-deps web npm run types:check   # types match the API
docker compose --profile manual run --rm --no-deps -e HOOPSEDGE_API_KEY=LEAKCHECK-KEY-7f3a9c \
  -e HOOPSEDGE_API_URL=https://leakcheck-api.invalid web sh -c "npm run build && npm run check:leak"
docker compose --profile manual run --rm --no-deps web-e2e sh -c "npm run build && npx playwright test"
docker compose --profile manual run --rm --no-deps web-e2e node scripts/lighthouse.mjs
```

The leak check and browser tests overwrite the dashboard's build folder, so stop `web` before them and start it again afterwards. Lighthouse measures the running dashboard, so `web` must be up for it. After regenerating the API schema, run `npm run types:generate`.
