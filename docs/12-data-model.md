# Reference — Data Model

## 1. DynamoDB: single table `hoopsedge-{env}`

Keys: `pk`, `sk` (strings). GSI1: `gsi1pk`, `gsi1sk`. TTL attribute: `ttl`.

| Entity | pk | sk | gsi1pk / gsi1sk | Key attributes | TTL |
|--------|----|----|-----------------|----------------|-----|
| Config | `CONFIG` | `v#{version}` and `CURRENT` | — | config JSON, changed_by, changed_at, diff | — |
| Lock | `LOCK#{league}#{slate}` | `LOCK` | — | run_id | 30 min |
| Run | `RUN#{run_id}` | `META` | `RUNS` / `{started_at}` | league, slate_date, trigger, status, steps[{name, status, started, ended, error}], counts | — |
| Run log | `RUN#{run_id}` | `LOG#{ts}#{seq}` | — | step, event (e.g. LEG_DROPPED), payload | 180 d |
| Game | `GAME#{league}#{game_id}` | `META` | `SLATE#{league}#{slate_date}` / `{tip_utc}` | teams, tip, status, scores, neutral | — |
| Prediction | `GAME#{league}#{game_id}` | `PRED#{run_id}` | — | model_version, calibrator_version, feature_set_version, feature_hash, ft/h1 params | — |
| Offer snapshot | `GAME#{league}#{game_id}` | `OFFER#{fetched_at}` | — | s3 pointer to the raw snapshot, normalized offers | 365 d |
| Scored outcome | `GAME#{league}#{game_id}` | `SCORED#{run_id}#{family}#{specifier}#{outcome}` | — | p_win, p_push, q_fair, edge, ev, eligible, reject_reason | — |
| Ticket | `TICKET#{ticket_id}` | `META` | `SLATE#{league}#{slate_date}` / `T#{family}#{ticket_no}#v{version}` | run_id, family, ticket_no, version, status (`pending_booking`/`dry_run`/`verified`/`invalid`/`superseded`), booking_code, selection_hash, combined_odds, combined_prob, ev, settle_status | — |
| Leg | `TICKET#{ticket_id}` | `LEG#{rank}` | `PENDINGLEG` / `{tip_utc}` (removed when settled) | game_id, event_id, market_id, specifier, outcome_id, pick label, odds, p_win, edge, status, actual_value | — |
| Unmatched | `UNMATCHED#{league}` | `{event_id}` | — | sb names, start, suggestions, status | 14 d after resolve |
| Team alias | `ALIAS#{league}` | `{alias_normalized}` | `TEAM#{team_id}` / `{alias}` | team_id, source, confirmed_by | — |
| Champion | `CHAMPION#{league}` | `CURRENT` | — | version, calibrator_version, promoted_at | — |
| Model audit | `MODEL#{league}` | `{ts}#{version}` | — | metrics, decision, reasons | — |
| Aggregates | `AGG#{league}#{family}` | `{period}` (e.g. `D#2026-11-04`, `ALL`) | — | legs, won, lost, push, calibration buckets | — |
| Cost | `COST` | `{date}` | — | mtd, forecast, by_service | 400 d |
| Alert | `ALERT` | `{ts}#{key}` | — | severity, message, ref | 90 d |

**Access patterns:**
- slate view → GSI1 `SLATE#…` (games + tickets)
- pending legs for grading → GSI1 `PENDINGLEG`
- recent runs → GSI1 `RUNS`
- everything else → direct pk queries

## 2. S3 layout `s3://hoopsedge-{env}-{acct}/`

```
raw/espn/{league}/{yyyy}/{mm}/{dd}/{game_id}.json         # lifecycle: IA 30d, expire 180d
raw/espn/injuries/{yyyy}/{mm}/{dd}/{ts}.json
raw/sportybet/{run_id}/{call}-{ts}.json
curated/{table}/league={L}/season={S}/part-*.parquet       # kept
curated/_deltas/{table}/{date}/*.parquet                   # compacted nightly
curated/_rejects/{table}/{date}.jsonl
curated/ratings_snapshots/league={L}/season={S}/{slate_date}.parquet
curated/features/league={L}/season={S}.parquet
models/{league}/{version}/...                              # see doc 08
reports/evidence/{league}/{date}.json|md|png
reports/learning/{league}/{date}.json
```

## 3. Parquet schemas (abridged; full pydantic models live in `common/schemas.py`)

- `games`: league, season, game_id, tip_utc, slate_date_et, home_id, away_id, neutral, venue_id, status, home_pts, away_pts, home_1h, away_1h, periods (list<int> per team), ot_periods, postseason, season_flag
- `team_games`: game_id, team_id, opp_id, is_home, fgm, fga, fg3m, fg3a, ftm, fta, oreb, dreb, tov, pf, pts, pts_1h, minutes, poss
- `player_games` (NBA): game_id, team_id, player_id, minutes, pts, starter
- `injuries` (NBA): captured_at, player_id, team_id, status, detail
- `features`: game_id, as_of, feature_set_version, feature_hash, …features…, targets
- `predictions_log`: run_id, game_id, model_version, ft/h1 params (written alongside DynamoDB, for analysis)

## 4. ID conventions

- `run_id` = ULID
- `ticket_id` = ULID
- `selection_hash` = sha256 of the sorted `{event_id, market_id, specifier, outcome_id}` tuples
- `feature_hash` = sha256 of canonical JSON of the feature values (floats rounded to 6 dp)
