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
