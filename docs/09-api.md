# Phase 8 — API (FastAPI on a Lambda Function URL)

**Goal:** a small, fast, read-mostly API for the dashboard. It is only reachable with the shared secret (D8).

## 8.1 Security model (no user auth)

- Every request must carry the header `x-hoopsedge-key`. The middleware compares it in constant time against the SSM `api_shared_secret`. A mismatch returns `401` and logs the source IP. No other details are leaked.
- Only the Next.js server (Route Handlers / Server Components on Vercel) holds the secret, as `HOOPSEDGE_API_KEY`. The browser never calls AWS directly.
- CORS is disabled; server-to-server calls don't need it.
- Mutations require `POST` plus an `idempotency-key` header. They are rate-limited in-app to 10 per minute.
- Reserved concurrency of 5 caps any abuse cost.

## 8.2 Endpoints

| Method | Path | Returns / does |
|--------|------|----------------|
| GET | `/health` | `{ok, env, champion_versions, last_runs}` (no secret needed, returns no data) |
| GET | `/slates/{date}` | per league: status, run summary, tickets grouped by market (latest non-superseded version), counts of eligible/rejected legs |
| GET | `/tickets/{ticket_id}` | ticket, legs (with probabilities, odds, edge, status), booking code, verification result, version history |
| GET | `/games/{game_id}` | prediction distributions (ft + 1h params), offers, all scored outcomes, key feature values with ratings, injuries (NBA), result if final |
| GET | `/runs?limit=` / `/runs/{run_id}` | pipeline runs with per-step timings, errors, dropped-leg log |
| GET | `/track-record?league=&market=&from=&to=` | leg hit rate, push rate, calibration buckets, ticket outcomes, closest-miss stats (legs won of N on lost tickets), flat-stake ROI per leg |
| GET | `/models/{league}` | champion, history of challengers with metrics and decisions, learning curve series |
| GET | `/evidence/{league}` | latest evidence JSON |
| GET | `/unmatched` | open unmatched events with suggestions |
| POST | `/unmatched/{id}/resolve` | `{team_id_home, team_id_away}` or `{ignore:true}` → writes confirmed aliases |
| POST | `/runs` | `{league, slate_date?}` → starts the pipeline (Run now). Returns `run_id`. A `409` if the lock is held |
| GET/PUT | `/config` | read / update config (version-checked; every change is written as an audit item) |
| PUT | `/schedules/{name}` | change a run time (validates HH:MM, updates EventBridge Scheduler) |
| POST | `/models/{league}/rollback` | `{version}` |
| GET | `/system` | cached daily cost, budget status, alert log, data freshness (last ingest, last odds fetch) |

All responses are typed with pydantic. An OpenAPI schema is generated, and the web app generates TypeScript types from it (`openapi-typescript`) in CI. A schema drift fails CI.

## 8.3 Performance & cost

- Use DynamoDB queries only (no scans) via the access patterns in doc 12.
- Track-record aggregates are precomputed by `grade`/`learn` into `AGG#` items. The API doesn't aggregate on the fly.
- Cold start: lazy imports (no LightGBM or polars in the API path).
- Response `Cache-Control: private, max-age=30`.

## Definition of Done

- [ ] All endpoints are implemented with tests (moto for DynamoDB).
- [ ] The 401 path is tested.
- [ ] Type generation is wired into CI.
- [ ] p95 latency < 300 ms warm.
