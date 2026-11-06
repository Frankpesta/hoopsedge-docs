# 00 — System Architecture

## 1. High-level diagram

```
                    ┌────────────────────── AWS (us-east-1) ──────────────────────┐
 ESPN public JSON ─▶│ Lambda: ingest  ─▶ S3 raw/ ─▶ curated Parquet              │
 (scores, box,      │      │                                                      │
  injuries)         │      ▼                                                      │
                    │ Lambda: ratings  (recomputed after every new final)        │
                    │      │                                                      │
 SportyBet NG  ◀───▶│ Step Functions "slate pipeline" (per league)               │
 (events, odds,     │   1 refresh ingest → 2 update ratings → 3 fetch SportyBet  │
  share/booking)    │   4 match teams → 5 predict → 6 score legs → 7 build       │
  [optional NG      │   tickets → 8 re-check odds → 9 book → 10 verify codes     │
   proxy]           │   → 11 persist + notify                                    │
                    │                                                             │
                    │ Lambda: grade (every 30 min)   Lambda: learn (nightly)      │
                    │ DynamoDB (state)   S3 (data/models/reports)   SSM (secrets) │
                    │ Lambda Function URL: FastAPI  ◀── secret header ──┐         │
                    └───────────────────────────────────────────────────┼─────────┘
                                                                        │
                                       Vercel: Next.js dashboard (server-side fetch)
                                                                        │
                                                               Owner's browser/phone
```

## 2. Components

| Component | Responsibility | Runtime |
|-----------|---------------|---------|
| `ingest` | Pull scoreboards, game summaries (box score, linescores, venue, neutral flag), NBA injuries; write raw JSON → curated Parquet; upsert `games` table | Lambda 1 GB, 5 min |
| `ratings` | Rebuild point-in-time ratings for the current season (ms-scale ridge solves) | Lambda 2 GB, 2 min |
| `slate pipeline` | Produce, book and verify tickets for one league + slate date | Step Functions + Lambdas |
| `grade` | Settle legs/tickets once games are final; trigger rating refresh | Lambda 1 GB, every 30 min |
| `learn` | Nightly recalibration, challenger training, evaluation, promotion | Lambda 10 GB, 15 min |
| `api` | Read models for dashboard + a few mutations (run now, resolve team, config, rollback) | Lambda 512 MB via Function URL |
| `costs` | Daily Cost Explorer pull, cached in DynamoDB | Lambda 256 MB, daily |
| `web` | Dashboard | Vercel |

## 3. Schedules (EventBridge Scheduler, timezone `Africa/Lagos`)

| Name | Cron | Action |
|------|------|--------|
| `grade-and-ingest` | every 30 min | ingest finals since last run → grade → refresh ratings if anything changed |
| `ncaa-slate` | `0 15 * * ? *` | slate pipeline, league=NCAA, slate=today ET |
| `nba-slate` | `30 19 * * ? *` | slate pipeline, league=NBA, slate=today ET |
| `nightly-learn` | `0 11 * * ? *` | learning job, both leagues (after all US games final) |
| `daily-costs` | `0 9 * * ? *` | cost snapshot |

Times live in `config` (DynamoDB). The CDK reads its defaults from config. Changing a time from the dashboard updates the schedule through the API, using the `scheduler:UpdateSchedule` permission on those schedules only.

## 4. Slate pipeline (Step Functions definition)

Each state is a Lambda task with a retry policy: 3 attempts, backoff rate 2, 5 s base, on transient errors only. A `Catch` routes failures to `MarkRunFailed`, which records the error and sends an alert. Each state's input is `{run_id, league, slate_date, trigger}`.

1. **AcquireLock**: a DynamoDB conditional put on `lock#{league}#{slate_date}`. It fails if a run is already in progress (TTL 30 min).
2. **RefreshIngest**: ingest latest finals and NBA injuries.
3. **UpdateRatings**: rebuild ratings as of now.
4. **FetchOffers**: fetch SportyBet basketball events for the league/slate, with all four market families. Store the raw snapshot in S3.
5. **MatchTeams**: map SportyBet events to canonical games. Unmatched events go to the queue and are excluded.
6. **Predict**: build features and run the champion models to get score distributions. Persist predictions.
7. **ScoreLegs**: for every offered outcome, compute model probability, fair implied probability, edge and EV.
8. **BuildTickets**: select legs and partition them into tickets per market (doc 06).
9. **RecheckOffers**: re-fetch odds for selected legs. Drop or re-score anything that moved. Rebuild tickets if needed.
10. **Book**: call the SportyBet share/booking endpoint per ticket. Skipped when `DRY_RUN`.
11. **VerifyCodes**: decode each code and compare selections. Mark the ticket `verified` or `invalid`.
12. **Finalize**: supersede older versions for the same league/slate/market/ticket number, release the lock, mark the run `succeeded`, notify.

## 5. Environments

| Env | Purpose | DRY_RUN |
|-----|---------|---------|
| `local` | Unit tests, scripts, fixtures | true |
| `dev` | Full AWS stack, booking disabled | true |
| `prod` | Live | false |

Both AWS stacks use the same CDK app with an `env` context. Before `prod` is deployed, `dev` must have run a complete pipeline for both leagues using real data.

## 6. Configuration (`config` item in DynamoDB, editable from the dashboard)

```jsonc
{
  "version": 7,
  "schedules": { "ncaa_slate": "15:00", "nba_slate": "22:00", "nightly_learn": "11:00" },
  "leagues": {
    "NBA":  { "enabled": true, "max_legs_per_ticket": 20, "max_tickets_per_market": 1 },
    "NCAA": { "enabled": true, "max_legs_per_ticket": 20, "max_tickets_per_market": 5, "max_legs_per_market": 100 }
  },
  "markets": {
    "handicap":   { "enabled": true, "min_prob": 0.56, "min_edge": 0.015 },
    "total_ft":   { "enabled": true, "min_prob": 0.56, "min_edge": 0.015 },
    "total_1h":   { "enabled": true, "min_prob": 0.56, "min_edge": 0.015 },
    "team_total": { "enabled": true, "min_prob": 0.56, "min_edge": 0.015 }
  },
  "min_legs": 2,
  "min_minutes_before_tip": 20,
  "allow_alt_lines": false,
  "min_team_games_for_full_confidence": { "NBA": 10, "NCAA": 8 },
  "sportybet_proxy_enabled": false,
  "dry_run": false,
  "pause_all": false
}
```

Thresholds are starting values only. Phase 4 backtests must justify them or change them, and the outcome is recorded in `docs/DECISIONS.md`.
