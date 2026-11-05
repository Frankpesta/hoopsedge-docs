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

A one-time download of past games from ESPN, then odds, into `data/raw/`. It's safe to stop and start again, and it doesn't restart by itself after a reboot. Starting it again resumes where it stopped.

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
