# Phase 2 — Data Ingestion & Team Registry

**Goal:** a complete, clean history of every game (past seasons + current season to date), kept up to date every 30 minutes, plus a canonical team registry. Schemas are in `12-data-model.md`.

## 2.1 Sources

As decided in Phase 0: ESPN public JSON (primary), and CBBD as the NCAA fallback if chosen. Everything goes through one HTTP client (`common/http.py`):
- per-host rate limit (default 2 req/s)
- retries with jittered exponential backoff on 429/5xx/timeouts (max 5)
- a descriptive User-Agent (generic client name; no personal email)
- an on-disk cache in local mode
- the raw response is always written to S3 *before* parsing, so parsing bugs can be replayed without re-fetching

## 2.2 Historical backfill (`scripts/backfill.py`, runs on the owner's machine)

- **Seasons:**
  - NBA: 2015-16 → current.
  - NCAA D1 men: 2015-16 → current.
  - Include the postseason. Mark the COVID-affected seasons with `season_flag`.
- **Steps per date:**
  1. scoreboard → list of game IDs
  2. summary for each final game
  3. write raw JSON to `s3://…/raw/{source}/{league}/{yyyy}/{mm}/{dd}/{game_id}.json`
  4. parse to curated rows
- **Resumable:** a checkpoint file per league/season, so it can be stopped and restarted.
- **Upload:** curated Parquet is written per league-season under `curated/` and uploaded with `aws s3 sync`.
- **Expected volume:** about 13k NBA games and about 60k NCAA games, a few GB raw. Running from the owner's machine keeps AWS costs near zero.

## 2.3 Curated tables (Parquet, partitioned by `league/season`)

- `games`: one row per game. IDs, tip time (UTC), slate date (ET), home/away team IDs, neutral flag, venue, status, final scores, 1st-half scores, period scores (array), OT count, postseason flag.
- `team_games`: two rows per game. Team box totals: FGM/FGA, 3PM/3PA, FTM/FTA, OREB, DREB, TOV, PF, PTS, 1H PTS, minutes (accounting for OT), and possessions computed as `FGA − OREB + TOV + 0.44·FTA` (averaged across both teams for the game).
- `player_games` (NBA only): player ID, team, minutes, PTS, starter flag.
- `injuries` (NBA only): snapshot rows `{captured_at, player_id, team_id, status, detail}`. These are captured on every ingest run and only appended, so point-in-time availability can be reconstructed.

Every parser has unit tests on recorded fixtures. Validation rules, where a failure means the row is quarantined to `curated/_rejects/` with a reason:
- the sum of periods equals the final score
- possessions fall in a plausible range (NBA 80–120, NCAA 50–90, scaled for OT)
- each team appears once per game

## 2.4 Live ingest (`ingest` Lambda, every 30 min, plus the first pipeline step)

- Scan the scoreboards for yesterday, today and tomorrow (ET), for both leagues.
- Fetch summaries only for games that are newly final, plus upcoming games when the venue/neutral info is missing.
- Upsert the `GAME#` items in DynamoDB. Append curated rows to the current season's Parquet: write a small delta file, compacted nightly by `learn`.
- Emit `new_finals=[...]`. If it is non-empty, `grade` and `ratings` run.
- NBA injuries: capture a snapshot each run.

## 2.5 Team registry (`teams/`)

- `teams` table: `team_id` (canonical, from ESPN), league, full name, short name, abbreviation, conference, `is_d1`, home venue coordinates where available.
- `team_aliases` table: `{league, alias_normalized, team_id, source, confirmed_by, confirmed_at}`.
- Seed aliases from ESPN's names (display, short, abbreviation, nickname).
- Normalization: lowercase, strip accents and punctuation, expand/contract `st`↔`state`, `univ`, `u`, `&`↔`and`, and parentheses (e.g. `miami (oh)`). Add a curated list of known NCAA hard cases: Miami FL/OH, St. John's, Saint Mary's, Loyola (Chi/MD/Marymount), USC vs South Carolina, UConn, UNC branches, Texas A&M branches, etc.
- Non-D1 opponents in NCAA games are kept under a single pooled `NON_D1` team for ratings purposes.

## Definition of Done

- [ ] Backfill completed for all target seasons. The coverage report shows ≥ 99% of games with half scores and box totals.
- [ ] Live ingest running in `dev` for 48 h without failures.
- [ ] Parser and validation tests pass on fixtures.
- [ ] Team registry seeded. Every D1 team has at least 3 aliases.
