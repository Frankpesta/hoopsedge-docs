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
