# Target "dev": the local toolchain for tests, linting and scripts.
# The arm64 Lambda target from docs/02-infrastructure.md is added at deployment.
FROM python:3.12-slim-bookworm AS dev

COPY --from=ghcr.io/astral-sh/uv:0.8.22 /uv /usr/local/bin/uv

# LightGBM's OpenMP runtime, which the slim image leaves out.
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# The virtual environment lives outside the mounted repo so nothing from
# Linux ends up in the Windows working tree.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/workspace/services \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /workspace/services
COPY pyproject.toml uv.lock ./
RUN uv sync --locked

# Target "collector": the early data collector (D13). The code is copied in, so
# switching branches in the working tree can't break a running collector.
# Rebuild it to pick up code changes.
FROM dev AS collector
COPY hoopsedge ./hoopsedge
CMD ["python", "-m", "hoopsedge.ingest.collector", "run"]
