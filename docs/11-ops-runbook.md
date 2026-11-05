# Phase 10 — Operations, Alerts & Go-Live

## 10.1 Alerts (SNS → owner email; optional Telegram bot later if the owner asks)

| Severity | Trigger |
|----------|---------|
| CRITICAL | pipeline run `failed`; any ticket `invalid`; booking error after retry; budget alert ≥ $25 |
| WARNING | > 5 unmatched events on a slate; drift monitor flag; ingest stale > 2 h during the season; nightly learn failed; score correction detected |
| INFO | daily digest (after the NCAA run): codes produced per league/market, legs excluded by reason, yesterday's results |

Alerts are deduplicated: the same key within 1 h is not sent again. Every alert is also written as an `ALERT#` item for the dashboard.

## 10.2 Runbook

| Symptom | Check | Fix |
|---------|-------|-----|
| SportyBet calls 403 / challenge page | raw responses in S3, `/system` freshness | Re-run Phase 0 geo test; enable proxy flag; if the API changed, update `endpoints.yaml`/parsers, then replay fixtures |
| Codes `invalid` | ticket verification diff | Usually a market/outcome ID change. Update `markets.yaml`, add a fixture, re-run |
| Many unmatched NCAA teams | `/unmatched` | Resolve in the UI (aliases persist). If it's systematic, add a normalization rule + tests |
| ESPN data missing | `curated/_rejects`, ingest logs | Re-run ingest for the date (`POST /runs` won't help; use the `ingest` Lambda with a `{dates:[...]}` payload) |
| Model looks off | drift panel, evidence | Roll back the champion; investigate features for a data issue |
| Cost spike | `/system`, Cost Explorer | Check Lambda invocation counts (runaway schedule?) and S3 request counts; reserved concurrency caps the blast radius |

## 10.3 Go-live checklist

- [ ] Phases 0–9 Definitions of Done all ticked.
- [ ] `dev` ran the full daily cycle (both leagues, all markets) for **7 consecutive days** with `DRY_RUN=true`. All pseudo-tickets were graded. No CRITICAL alerts.
- [ ] Evidence report reviewed by the owner. Market gates confirmed.
- [ ] One supervised live booking per league, verified, with codes checked manually in the SportyBet app.
- [ ] Budgets confirmed; the month-to-date cost projection is under $10.
- [ ] `prod` deployed and schedules enabled. `dev` schedules disabled to save credits.
- [ ] The README tells the owner how to: change thresholds, resolve unmatched games, roll back a model, pause everything (a single "pause all" config flag the pipeline honours).

## 10.4 Season calendar

- NBA regular season Oct–Apr, then playoffs (fewer games; NBA-only tickets).
- NCAA Nov–Mar. Conference tournaments and the NCAA tournament take place on neutral sites, so check the neutral flags.
- Off-season: the `pause_all` flag stops schedules (no compute cost). Ingest stays on weekly for roster/prior updates before the next season.
