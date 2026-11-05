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
