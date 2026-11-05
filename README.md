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
