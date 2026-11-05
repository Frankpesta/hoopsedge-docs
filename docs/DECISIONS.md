# Decisions Log

Append-only. Each entry: date, decision, reason, who. Claude Code adds entries whenever reality differs from the docs.

## 2026-11-04 — Initial decisions (owner-approved plan)

| ID | Decision | Notes |
|----|----------|-------|
| D1 | Current-season inputs + decaying preseason prior | Owner emphasised current-season data; prior needed because NCAA started this week |
| D2 | Four markets (handicap, FT O/U, HT O/U, team O/U), separate codes each | Owner request |
| D3 | NCAA: top 100 legs per market → tickets of 20; applies to every market | Owner answer to Q6 |
| D4 | Drop legs below confidence threshold even if tickets are short | Owner answer to Q7 |
| D5 | Main line only by default; alternate lines behind a flag | Recommended default; owner can change in Settings |
| D6 | Auto daily runs (NCAA 15:00, NBA 19:30 Lagos) + Run now | Recommended default; times editable |
| D7 | NG proxy only if SportyBet blocks AWS; owner approves any paid proxy | Recommended default |
| D8–D11 | No user auth (shared server secret), continuous learning with champion/challenger, one leg per game per ticket, never place bets | Owner requirements |

## Pending (filled during build)

- Phase 0: SportyBet endpoints, market IDs, geo result, max selections per booking
- Phase 0: data-source choice for NCAA (ESPN vs CBBD); historical lines availability
- Phase 4: chosen distribution (normal vs empirical), tuned thresholds, market gates

## 2026-11-05 — Build-plan decisions (owner-approved)

Item references (C1–C5, Q1–Q3) are in `docs/BUILD-PLAN.md`.

| ID | Decision | Reason | Who |
|----|----------|--------|-----|
| D12 | Use a new, separate AWS account with a `hoopsedge` CLI profile. Do not use `frank-fps-deploy`. | Keeps credits, budgets and permissions isolated (Q1) | Owner |
| D13 | Run an early data collector from Phase 0, as a local Docker service writing to `./data`, until AWS ingest takes over. Exception to the phase order. | Injury and odds history can't be recovered later, and both seasons are underway. Gaps are accepted when the machine is off (Q2) | Owner |
| D14 | A result is usable only if final and `result_known_at < as_of`, with `result_known_at = tip + 3 h` in replay. Training and backtests use the slate's scheduled run time as `as_of`. Stricter than invariant 1, which stays in force. | Stops backtests seeing results that live runs can't see (C1) | Owner |
| D15 | V1 trains with the NBA injury feature null and relies on the `NBA_INJURY_UNCERTAIN` gate. The feature activates through the normal challenger process once enough snapshots exist. | No historical injury snapshots exist, and using actual absences would break invariant 1 (C2) | Owner |
| D16 | Compare champion and challenger only when the champion's training cutoff is before the holdout start; otherwise skip and record why. | The champion may have trained on the holdout, making the test unfair. Promotion happens about every 21 days at most (C3) | Owner |
| D17 | Score all candidate models on one shared line set: real market lines where available, otherwise baseline median ± offsets. The baseline's σ is the league constant from training seasons. | Scoring each model at its own median isn't like for like (C4) | Owner |
| D18 | Calibrate on real market lines where available. If history has none, label market-edge claims "unproven" until live logs build up, and rely on the market gates meanwhile. | Selected legs are where the model most disagrees with the market, which synthetic-line calibration doesn't correct (C5) | Owner |
| D19 | The dashboard URL stays open with no Vercel protection, as D8 specifies. | Owner's choice. Accepted risk: anyone with the URL can use Run now, change config, turn off dry-run or roll back a model (C14, Q3) | Owner |
| D20 | Build and run locally in Docker first, and deploy to AWS when the build is done. Phase 1 (infrastructure) moves to the end. Definitions of Done that say `dev` are met locally first, then re-checked after deployment. Invariants are unchanged. | Owner's direction. Known consequence: the Phase 0 AWS geo test also moves later unless run separately, so a SportyBet block on AWS IPs would surface late | Owner |
| D21 | Each feature gets its own branch. Claude Code leaves the work uncommitted, the owner commits it, and only then does the next feature start. | Owner's direction | Owner |
| D22 | All AWS work waits until the local build works end to end. That covers the credit activities, the `hoopsedge` profile check, the Lambda limit check and the early AWS geo test. Nothing has been created in the new account. | Owner's direction. Known consequences: the AWS account is on the Free plan, which ends after 6 months or when credits run out, and the credit activities must be done within 6 months of sign-up. The account must be upgraded before that, because HoopsEdge has to run longer. The geo result stays unknown until deployment (see D20) | Owner |

## 2026-11-05 — ESPN probe findings (awaiting owner review)

Evidence is in `docs/reports/phase0-data-coverage.md`. Full-season scans cover 2015-16 and 2024-25. Box scores and lines are samples: up to 150 and 60 games in those two seasons, and 12-15 games from 2-3 dates in each season row.

| ID | Decision | Reason | Who |
|----|----------|--------|-----|
| D23 | Exclude NBA exhibitions at ingest: competition type `ALLSTAR`, or a note headline containing "all-star" or "rising stars". Phase 2 also rejects any game whose teams aren't in the team registry. NBA Cup, international and rescheduled games stay. | The docs don't mention these. ESPN files them as regular-season games (type 2), some with the standard competition type. Teams such as World or Team Shaq, with games as short as about 30 possessions, would pollute ratings. The rule caught 1 event in the 2015-16 scan and 3 in 2024-25, and half-score coverage is 100% once they're removed | Claude Code |
| D24 | Compute TOV as player turnovers plus `teamTurnovers`. Don't use ESPN's `totalTurnovers`. | Differs from a direct reading of docs/03 section 2.3. On sampled dates in December 2017 and January 2018, `totalTurnovers` reads 0-2 while players sum to 9-22, which understates possessions. With the fix, every sampled NBA season is 100% plausible (one row was 76.92% before) | Claude Code |
| D25 | Proposed: when a final NBA game has an empty team box, keep its score, flag it `box_missing`, and leave it out of efficiency and tempo updates. Never impute it. Whether training starts at 2015-16 or later is decided in Phase 4 on measured effect. | About 14% of sampled 2015-16 games (21 of 149) and 2 of 15 in each of 2016-17 and 2017-18 have no team or player box stats. None were seen in samples from 2018-19 on. One NCAA game in a 2024-25 sample had the same problem. The core statistics endpoint returned 404 for the one game tried; no other source has been checked | Claude Code, needs owner review |
| D26 | Use ESPN for both leagues. CollegeBasketballData.com is not needed for scores or box scores. | Half scores are 100% in full scans of both leagues and both seasons, above the 99% target. Scoreboards return games for every season back to 2015-16 (COVID seasons get flagged in Phase 2). The NCAA `groups=50` count was not checked against an outside source; the busiest days had 155 and 199 games | Claude Code |
| D27 | Historical lines come from the ESPN core odds API, full-game spread and total only, excluding non-book and in-play feeds. HT and Team O/U have no historical lines, so their edge stays "unproven" (D18) until SportyBet snapshots build up. A negative spread means the home team is favoured. | Sampled coverage: NBA 100% in every season; NCAA 53-100%, including 60% in this season's first days. Open and close prices exist only from 2023-24; before that only `current`, whose timing isn't verified, so it must not be treated as a closing line. Free closing-line archives (docs/01 section 0.4, step 2) were not investigated | Claude Code |
| D28 | The 2 requests/second limit stands. | The run that fetched the data made 2,226 requests: 2,217 returned 200, none returned 429, and 9 failed with connection errors and were retried. About 240 further responses came from the earlier stopped run, whose rate figures weren't kept | Claude Code |
| D29 | Treat `injuries` in ESPN game summaries as current, never historical. Only the injury snapshots we collect ourselves count as history. Phase 3 must map the live statuses to availability weights. | Confirms D15: every injury row in sampled past seasons is dated after tip-off. Docs/04 expects Out, Doubtful and Questionable, but the live endpoint shows Out and Day-To-Day (fantasy status: OUT, GTD, OFS). That's one snapshot, so other statuses may still appear. NCAA samples had no injury rows | Claude Code, needs owner review |

## 2026-11-05 — SportyBet discovery findings (awaiting owner review)

Evidence is in `docs/reports/phase0-sportybet-discovery.md`, from one snapshot at about 15:40 UTC, taken from the owner's machine. IDs and URLs are in `services/hoopsedge/sportybet/`. The owner approved each test code; 4 of the 5 permitted were created.

| ID | Decision | Reason | Who |
|----|----------|--------|-----|
| D30 | Use the endpoints in `endpoints.yaml`, with no session bootstrap and a descriptive User-Agent. Check `bizCode` on every response. | The doc 01 candidates were right. Reads and bookings worked with no cookie or token, and only `OperId: 2` was added on booking. Docs/07 expects browser-like headers and a possible cookie; neither was needed. Failures return HTTP 200 | Claude Code |
| D31 | Market IDs: handicap 223, full-game total 225, first-half total 68, team totals 227 (home) and 228 (away). Never use market 18 for `total_ft`. | The first-half market's site name is "1st half - total", not the name in docs/01. Market 18 is also labelled "Over/Under" but covers regular time only, so it would be graded against the wrong score. Overtime treatment comes from SportyBet's market text; settlement hasn't been observed | Claude Code |
| D32 | Main line: the bookable line with the most even prices. Ties go to the `favourite` flag, then to the first specifier. | Implements D5, which doesn't cover ties. Neighbouring lines often tie (1.75/1.90 and 1.90/1.75). On the first slate date the rule chose `favourite` on 217 of 218 checks; the exception was a suspended favourite, so the flag can't be used alone | Claude Code |
| D33 | Fetch offers with one bulk list call per tournament, not per event. Resolves C10. Always check market `status`. | Each call returns all lines of the five markets. On the 3 events compared, the list contained every line the detail call had, plus locked extras. Three events is a small sample, so Phase 6 validation must still fail loudly on an unexpected shape | Claude Code |
| D34 | D10 (one leg per game) is enforced only by our ticket builder, with a test. `min_legs` and the 20-leg split stand. | SportyBet accepted two legs from one game, so it provides no backstop. A 20-leg code booked and verified. The site's config says the maximum is 50, which wasn't tested and isn't needed | Claude Code |
| D35 | Proposed change to docs/07 section 6.4: when a booking is rejected, don't assume the bad leg can be identified. Re-fetch offers, drop legs that are no longer bookable, and retry once. If it still fails, mark the ticket failed and alert. | Differs from docs/07, which says to drop the rejected leg. A rejection returned only `bizCode` 19000 and a message, with no leg named. Only a fake outcome ID was tested, so suspended lines and changed odds may behave differently | Claude Code, needs owner review |

Open items from this discovery:

- **NCAA volume (affects D3):** 43 of ESPN's 65 games were listed for 2026-11-05, and 40 had all five markets bookable. Reaching 100 legs per market may be rare on days like this, which D4 accepts. The collector should measure how listings grow through the day before NCAA's 15:00 Lagos run time is relied on.
- **Geo:** the owner's machine passes. AWS stays untested until deployment (D20, D22).
- **Status values:** suspended and postponed events weren't observed. Anything except status 0 is treated as not bookable.

## 2026-11-05 — Early data collector (awaiting owner review)

| ID | Decision | Reason | Who |
|----|----------|--------|-----|
| D36 | Collector intervals: SportyBet offers every 30 min, NBA injuries every 60 min, ESPN schedule and pre-game lines every 6 h. Snapshots follow the doc 12 raw layout, gzipped, under `data/raw/`. Every attempt is logged to `data/collector/runs.jsonl`. | D13 set no intervals. Offers change most and can't be recovered, and 30 min also measures listing growth. ESPN's core API keeps open and close prices from 2023-24 (D27), so its lines are a backup. First-pass storage implies about 6 MB a day; busier slates will use more | Claude Code |
| D37 | A 401, 403, 451 or non-JSON response halts that source, even across restarts, until the owner runs `resume`. Other failures are logged and retried at the next interval. | Applies the doc 01 rule to stop rather than work around bot protection. A long-running service must survive a passing outage, and a failed run stays visible in `status`. There's no alert channel locally, so failures only show up there and in the logs | Claude Code |
| D38 | Correction to the NCAA volume item above: the 22 unlisted games aren't a SportyBet gap. All 22 have no ESPN odds either, and they are mostly D1 teams hosting smaller-college opponents. All 43 games with ESPN odds are listed on SportyBet. | Name-matched from one snapshot of 2026-11-05, with 2 matches confirmed by eye because the names differ (LIU, East Texas A&M). The opponents weren't checked against a registry. Volume is still limited by how many games are priced, so 100 legs per market stays unlikely on a day like this | Claude Code |

## 2026-11-05 — Phase 2 backfill

| ID | Decision | Reason | Who |
|----|----------|--------|-----|
| D39 | NBA history starts at 2018-19, not 2015-16. NCAA stays at 2015-16. Replaces D25's "decide in Phase 4" and differs from docs/01 and docs/03. Any game that still has an empty box keeps its score and is flagged `box_missing`. | Owner's answer on D25: about 13-14% of sampled NBA games from 2015-16 to 2017-18 have no box stats. Known consequences: NBA training has 3 fewer seasons, and 2018-19 starts with no preseason prior from an earlier season. One NCAA sample in 2024-25 also had no box, so the flag is still needed | Owner |
| D40 | Fetch in two stages: all games first, then core-API odds (D27) for every final game except NCAA games against non-D1 teams. | Summary `pickcenter` can't replace the odds call. In sampled dates it held only non-book providers (consensus, numberfire, teamrankings) in 2015-16 and 2020-21, and was empty in 2024-25. An earlier comparison showing 146 matches was on those non-book providers, so it didn't support the shortcut. Stopping after stage one still leaves complete game data. Skipping non-D1 games rests on one day's evidence (D38) | Claude Code |
| D41 | Every D1 team gets at least 3 aliases, except Duke, Elon, Ohio, Rice and Yale, which get 2. | Differs from docs/03's Definition of Done. Their display, short and location names normalize to the same text, and they have no genuine third name. An invented alias adds no matching power. Other teams get unique mascots and real alternates (`teams/hard_cases.yaml`) | Claude Code |
| D42 | Team IDs are unique only within a league. Always key teams by league and ID together. | ESPN reuses IDs: `2` is both Auburn and the Boston Celtics. Found when a check script that looked up names by ID alone printed "Auburn 93 at Brooklyn 104". Curated tables keep league with ID, and a test covers it, but Phase 3 onward must not join on team ID alone | Claude Code |
| D43 | The backfill stops 2 days before today, and live ingest covers yesterday onward. It also rejects a box whose field goals and free throws don't add up to the score, and counts play-in games (season type 5) as postseason. | A date once done is never revisited, so yesterday's unfinished late game would otherwise be lost. The points check is stricter than docs/03: it catches a box attached to the wrong team. Docs don't mention play-in games | Claude Code |
| D44 | Additions to doc 12: `games` gains `box_missing` and splits periods into `home_periods` and `away_periods`. New table `team_seasons` holds D1 membership per season, from ESPN's per-season lists. Raw files are gzipped (`.json.gz`), with scoreboards and odds beside each game. Season is the closing year (2025 is 2024-25). The fetch command is `hoopsedge.ingest.backfill`, not `scripts/backfill.py`, so it can run from an image. | Point-in-time D1 status can't come from the current team list: 351 teams in 2015-16, 366 now. An image-based run survives branch switches during a long download. A curated table of historical lines is left to Phase 4, which is its first user; the raw files are kept | Claude Code |
| D45 | The backfill and the collector don't share a rate limiter, so ESPN can briefly see more than 2 requests per second. | Differs from CLAUDE.md's per-host limit during overlaps: about 80 extra requests over 35 seconds every 6 hours, plus 1 an hour. A shared limiter isn't worth building for a one-time job, and the client backs off on any 429 | Claude Code |

## 2026-11-05 — Phase 2 live ingest

| ID | Decision | Reason | Who |
|----|----------|--------|-----|
| D46 | Live ingest scans from the day after the last settled date through tomorrow, always including yesterday. A date is settled once every game on it has finished; its scoreboard then goes where the backfill keeps them. With no settled date in the last 14 days, it goes back 14 days only. | Wider than docs/03's yesterday-to-tomorrow. Finals can be recovered, so days the machine was off shouldn't be lost. After more than 14 days off, re-running the backfill fills the gap. `backfill build` also picks up settled dates, which replaces their deltas | Claude Code |
| D47 | Files decide what has been processed, not the database. Locally, DynamoDB is `moto_server` (BUILD-PLAN section 3), and objects are plain files in docs/12's S3 layout under `data/`. There's no S3 client or Lambda handler yet; both come with deployment (Phase 1), so that wiring is untested. | moto forgets everything on restart, so "already processed" can't live there. Keeping curated data in moto would lose it too. This is the first thing Phase 1 must add, and the `ingest_once` function it will wrap is what the slate pipeline's first step calls | Claude Code |
| D48 | A game with no box stays in the scan while its date does, and is re-fetched each run until the box appears. Score changes on yesterday or later are flagged as corrections, overwrite the row, and appear in `new_finals`. Older corrections aren't detected here. | Not in docs/03, which fetches a summary once. Otherwise a box that lags the final whistle would leave a game `box_missing` for good. Docs/11 expects corrections to be detected, and rewriting a one-game delta file can't leave duplicates | Claude Code |
| D49 | The curated `injuries` table has one Parquet part per raw snapshot, including the collector's, and keeps snapshots with nobody injured. A row with no readable player ID is counted, not guessed. An unreadable snapshot is reported once and set aside. | Point-in-time lookups must see "nobody out" as a real state. Using the collector's files means history since 2026-11-05 counts. While both run, ESPN gets injury calls from both, which is a small and temporary overlap (see D45) | Claude Code |
| D50 | Live ingest shares one halt with the backfill: a block on any ESPN call stops both until `clear-halt` or `resume`. It also rejects any local run without an explicit database endpoint. | Same host, so a block on one concerns both. The backfill's running image only checks the halt when it starts a season, so it could continue briefly. The endpoint check means a local run can't reach real AWS by accident | Claude Code |
