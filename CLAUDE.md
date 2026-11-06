# HoopsEdge — Master Build Instructions for Claude Code

> Read this file fully before writing any code. Then read `docs/00-architecture.md` and the doc for the phase you are working on. Build **one phase at a time**, in order, and do not start a phase until the previous phase's "Definition of Done" is met.

## 1. What we are building

HoopsEdge is a **private, single-user** basketball prediction system for the **NBA** and **NCAA Men's D1** basketball. It:

1. Ingests every completed game of past and current seasons (scores, half scores, box scores, schedules, injuries).
2. Maintains point-in-time team ratings that update after **every** completed game.
3. Predicts each upcoming game as a **joint distribution of both teams' scores** (full game and first half).
4. Turns those distributions into probabilities for SportyBet markets:
   - **Handicap** (point spread)
   - **Full-time Over/Under** (game total)
   - **Half-time Over/Under** (1st-half total)
   - **Team Over/Under** (team totals)
5. Selects the strongest legs per market. It never forces weak legs into a ticket.
6. **Books** (never places) accumulator slips on SportyBet Nigeria and returns **booking codes**:
   - **NBA**: 1 ticket per market (up to 4 codes per slate).
   - **NCAA**: up to 100 games per market, split into tickets of max 20 legs. That is up to 5 tickets per market, so up to 20 codes per slate.
7. Grades every leg and ticket after games finish, recalibrates and retrains nightly, and only promotes a new model if it beats the current one on recent unseen games.
8. Displays everything in a beautiful Next.js dashboard on Vercel. There is no authentication; it is an internal tool.

Hosting: backend, data, models and schedules run on **AWS** (credit-conscious, see §4). The frontend runs on **Vercel**.

## 2. Product decisions (approved by the owner)

| # | Decision | Value |
|---|----------|-------|
| D1 | Data emphasis | Model inputs for a game use **current-season data up to that game's tip time**. Early in the season, each team's rating is blended with a **preseason prior** (last season's final rating, regressed to the mean). The prior's weight decays automatically as current-season games accumulate. Past seasons are used to *train* the model, never as stale team inputs. |
| D2 | Markets | Handicap, FT O/U, HT O/U, Team O/U. Each market is its own set of codes. |
| D3 | NCAA volume | Applies to **every** market. Take up to the **top 100** eligible legs (ranked by model probability) and split them into tickets of **20**. |
| D4 | Weak legs | Legs below the confidence threshold are **dropped**, even if a ticket ends up shorter than 20. A ticket needs at least `min_legs` (default 2) legs or it is not booked. |
| D5 | Lines | **Main line only** by default: the specifier whose two outcome prices are closest to even. Alternate lines sit behind the config flag `allow_alt_lines` (default `false`). |
| D6 | Timing | Automatic daily runs per league at configurable Africa/Lagos times (defaults: NCAA 15:00, NBA 22:00, changed by the owner in D71). There is also a **Run now** button. Re-runs create new ticket *versions*; old ones are marked `superseded`. Nothing is ever overwritten. |
| D7 | Geo access | If SportyBet NG blocks AWS IPs, only the SportyBet client is routed through a small Nigerian proxy (feature flag `sportybet_proxy_enabled`; proxy URL stored in SSM). |
| D8 | Auth | No user auth. The AWS API is protected by a shared secret that only the Vercel server knows. The browser never sees it. |
| D9 | Learning | Ratings update after every game. Nightly recalibration plus retraining runs a champion vs challenger comparison, and the challenger is promoted only if it is measurably better. |
| D10 | Same-game rule | At most one leg per game per ticket. For Team O/U, the system picks the single best of {home over, home under, away over, away under}. |
| D11 | Placing bets | **Never.** The codebase must not contain any login, wallet or bet-placement capability. |

## 3. Non-negotiable invariants ("foolproof" rules)

Each invariant must have an automated test. A phase is not done if one of its invariants is untested.

1. **No leakage.** A feature for game G may only use games whose tip time is strictly before G's tip time. There is a dedicated leakage test that fails the build.
2. **No started games.** A leg is never booked for a game that has started or tips within `min_minutes_before_tip` (default 20) of booking.
3. **Fresh odds.** Lines and odds are re-fetched no more than 5 minutes before booking. If a specifier or price changed, the leg is re-scored. If it falls below threshold, it is dropped.
4. **Verified codes.** Every booking code is decoded back through SportyBet's share lookup. Its selections are compared one by one (event, market, specifier, outcome) with what we intended to book. Any mismatch marks the ticket `invalid`, raises an alert, and the dashboard shows it as invalid.
5. **No guessed team matches.** The system auto-matches only when the match is unique and high-confidence. Anything else goes to the **Unmatched** queue for one-click confirmation. Unmatched games are excluded, never guessed.
6. **Idempotent and traceable.** Every prediction stores the model version, a feature-snapshot hash and the odds snapshot. Every ticket stores its run ID. Re-runs supersede earlier versions; they never mutate them.
7. **Fail loudly.** Any failed step marks the run `failed`, keeps partial artifacts and sends an alert. Silent fallbacks are forbidden. The only exception is explicitly documented ones such as "skip this game and record why".
8. **Dry-run first.** `DRY_RUN=true` runs everything except the actual SportyBet booking call. It is the default in local and dev environments.

## 4. AWS cost rules (credits must last)

Never introduce any of the following without the owner's written approval: NAT Gateway, RDS/Aurora, ElastiCache, OpenSearch, SageMaker endpoints, ALB/NLB, EC2 always-on instances, Secrets Manager, VPC-attached Lambdas, X-Ray, CloudWatch detailed metrics or dashboards.

Required practices:
- **Compute.** All Lambdas run on arm64 (Graviton) and outside a VPC. Use one shared Python container image with a different handler per function. ECR keeps only the last 3 images (lifecycle rule).
- **Storage and secrets.**
  - DynamoDB in on-demand mode. Use TTL on ephemeral items.
  - S3 lifecycle: raw JSON moves to Infrequent Access after 30 days and expires after 180. Curated Parquet is kept.
  - SSM Parameter Store **standard SecureString** for secrets (free).
- **Logging.** CloudWatch log retention is 14 days on every log group.
- **Scheduling and orchestration.**
  - Schedules use EventBridge Scheduler.
  - Pipelines use Step Functions Standard.
  - Training runs in Lambda (10 GB memory, 15-minute limit). Fargate **Spot** is a documented fallback, only if training exceeds 12 minutes.
- **Spend controls.**
  - Reserved concurrency caps on every function.
  - AWS Budgets alerts at $10, $25 and $50 per month.
  - A Cost Explorer query **once per day**, cached. The dashboard never calls it live.
- **Default region:** `us-east-1`. Phase 0 may justify another region.
- Every resource is tagged `project=hoopsedge`.
- Target cost: **under $10/month** excluding the optional proxy.

## 5. Tech stack

| Layer | Choice |
|-------|--------|
| Data/ML services | Python 3.12, `uv`, pydantic v2, httpx, polars and pyarrow, numpy/scipy, scikit-learn, LightGBM, rapidfuzz, boto3, structlog |
| API | FastAPI + Mangum on a Lambda Function URL |
| Orchestration | Step Functions + EventBridge Scheduler |
| Storage | S3 (raw JSON, curated Parquet, models, reports) and DynamoDB (operational state) |
| Infra as code | AWS CDK v2 (TypeScript) |
| Frontend | Next.js 15 (App Router, TypeScript), Tailwind CSS, shadcn/ui, Framer Motion, Recharts, deployed on Vercel |
| Tests | pytest + hypothesis (property tests) + recorded HTTP fixtures (respx); Vitest + Playwright for web |
| Quality | ruff, mypy (strict on `services/common`), ESLint, Prettier, GitHub Actions CI |

## 6. Repository layout

```
hoopsedge/
├── CLAUDE.md
├── docs/                      # this build pack
├── infra/                     # CDK app (TypeScript)
├── services/
│   ├── Dockerfile             # single Lambda image (arm64)
│   ├── pyproject.toml
│   └── hoopsedge/
│       ├── common/            # config, logging, s3, dynamo, ids, time (ET/Lagos), errors
│       ├── ingest/            # ESPN clients, parsers, curated writers
│       ├── teams/             # canonical teams, aliases, matcher
│       ├── ratings/           # adjusted efficiency, tempo, Elo, priors
│       ├── features/          # point-in-time feature builder
│       ├── models/            # score & half models, variance, calibration, registry
│       ├── markets/           # distribution → market probabilities, push/void logic
│       ├── engine/            # leg scoring, selection, ticket building
│       ├── sportybet/         # events client, market resolver, booking, verification
│       ├── grading/           # settle legs/tickets
│       ├── learning/          # nightly retrain, recalibrate, champion/challenger
│       ├── backtest/          # walk-forward simulation & evidence reports
│       ├── api/               # FastAPI app
│       └── handlers/          # thin Lambda entrypoints
├── scripts/                   # local one-off jobs (backfill, discovery)
├── web/                       # Next.js dashboard
└── .github/workflows/
```

## 7. Build order

| Phase | Doc | Output |
|-------|-----|--------|
| 0 | `docs/01-phase0-feasibility.md` | Verified SportyBet endpoints/markets, geo test, data-source proof, recorded fixtures |
| 1 | `docs/02-infrastructure.md` | CDK stacks, budgets, CI |
| 2 | `docs/03-data-ingestion.md` | Backfill + daily ingest, curated Parquet, team registry |
| 3 | `docs/04-ratings-features.md` | Point-in-time ratings and features + leakage test |
| 4 | `docs/05-models-backtesting.md` | Score/half models, variance, calibration, walk-forward evidence |
| 5 | `docs/06-prediction-ticket-engine.md` | Market probabilities, leg selection, ticket builder |
| 6 | `docs/07-sportybet.md` | Event fetch, matching, booking, code verification |
| 7 | `docs/08-grading-learning.md` | Grading, continuous learning, model registry |
| 8 | `docs/09-api.md` | FastAPI on Lambda URL |
| 9 | `docs/10-dashboard.md` | Next.js dashboard on Vercel |
| 10 | `docs/11-ops-runbook.md` | Alerts, runbook, go-live checklist |
| — | `docs/12-data-model.md` | Reference: tables, S3 layout, schemas (read alongside every phase) |

## 8. Working rules for Claude Code

- Write tests for the invariants **before** the implementation they protect.
- Never hardcode SportyBet market IDs, outcome IDs or team names in logic. They live in config or tables populated by Phase 0 and Phase 2 discovery.
- All times are stored in UTC (ISO 8601). Display uses Africa/Lagos. "Slate date" means the **US Eastern** calendar date of tip-off.
- Be polite to external sites: rate limits (default 2 requests per second per host), retries with jittered backoff, caching, and a descriptive User-Agent.
- If anything in these docs proves wrong once real data is in hand (e.g., an endpoint differs), stop, write down the discrepancy in `docs/DECISIONS.md`, adapt, and keep the invariants intact.
- Ask the owner before adding any paid service or any AWS service not listed in §4–§5.
