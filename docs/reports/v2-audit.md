# HoopsEdge v2 audit

Written 2026-11-22, from `main` at 78b2a1d, for the owner to review before any v2 change (brief §1).
The brief is `HOOPSEDGE_V2_REBUILD.md` at the repository root. Section numbers like §4.4 refer to it.

## 1. What exists today

All v1 phases are built and merged, apart from the serverless part of Phase 1.

| v1 phase | State | Where |
|---|---|---|
| 0 Feasibility | Done. SportyBet endpoints, markets and booking verified, 5 of 5 test codes used | `docs/reports/phase0-*`, `sportybet/discovery.py` |
| 1 Infrastructure | **Replaced by D80/D81.** No CDK or Lambda. One EC2 instance (`m7i-flex.large`, eu-north-1) runs the Docker Compose stack, with Caddy for HTTPS. SportyBet goes through the owner's laptop relay, because CloudFront challenges the app from AWS | `deploy/ec2/`, `deploy/relay/` |
| 2 Ingest | Done for NBA and NCAA men's basketball: backfill, live ingest every 30 min, collector snapshots, curated Parquet | `ingest/` |
| 3 Ratings and features | Done, with a leakage test | `ratings/`, `features/` |
| 4 Models and backtests | Done: LightGBM residual models, calibrators, evidence reports | `models/`, `backtest/` |
| 5 Ticket engine | Done: probability-based legs, thresholds, market shrink (D79) | `engine/`, `markets/` |
| 6 SportyBet | Done: offers, matcher, booking, code verification, live test | `sportybet/`, `teams/`, `engine/recheck.py` |
| 7 Grading and learning | Grading done. Learning (champion/challenger) done | `grading/`, `learning/` |
| 8 API | Done: FastAPI behind the shared secret | `api/` |
| 9 Dashboard | Done: Next.js, 10 routes. Vercel deployment pending at last record | `web/` |
| 10 Go-live | 9 real verified codes on 2026-11-06 from Run now (D78) | |

Tests: 36 test modules in `services/tests`, including `test_leakage.py` and `test_no_betting_guard.py`.
**There is no cost assertion test.** Brief §11.7 says "the cost assertion test from v1 still passes",
but none exists, because the CDK stacks it would check were never built. Proposal: drop that item, or
replace it with a check on the EC2 compose file (memory limits, log rotation).

## 2. Checks against the owner's NCAAF sheet

The owner's `ncaaf_predictions_20261121.csv` has 46 rows. Recomputed from its L5 PF/PA columns:

- **Model Total, Sum of Avgs, Lean (4.0 / 5.0) and Atk/Def tiers match on all 46 rows.** The model in §4
  is exactly what produced the sheet.
- **Glossary Outlook can be derived after all, against §4.4.** This rule matches all 46 rows:
  each team scores Attack High +1, Low −1, and Defence Low +1, High −1. Sum both teams. −2 or less is
  LOW, +2 or more is HIGH, anything else MEDIUM. 🛑 Owner to confirm.
- **The sheet pins the thresholds only loosely.** Every lean holds for any over threshold above 3.8 up
  to 4.3, and any under threshold above 4.8 up to 6.8 (12 OVER, 4 UNDER, 30 PASS). 4.0 / 5.0 are a valid
  pick, not a measured optimum. NCAAB and NBA thresholds have no evidence yet.
- The 21 November games have finished, so once ingest and the model exist, the sheet can be graded
  against ESPN finals as a first result for the hand-run model.

## 3. Problems in the brief to settle before building

### 3.1 Out of date

1. **The AWS SportyBet test is done (§5.1, Phase 0).** D81 found that CloudFront's WAF challenges the
   app's requests from EC2, so SportyBet goes through the laptop relay. Phase 0 only needs to re-run
   the upcoming list, one event and a dry-run payload through the relay, without booking.
2. **The relay is a single point of failure.** Every scheduled run (§12: 14:00, 16:00, 18:00, 21:00 WAT)
   needs the owner's laptop on and connected. The brief doesn't mention it. A failed relay must fail the
   run loudly, as it does today, and the dashboard should show relay health.
3. **"Keep infrastructure" means the EC2 stack**, not the CDK plan in `docs/02`.

### 3.2 Contradictions

4. **State is lost on restart.** `aws-mock` is `motoserver/moto` in memory, on EC2 as well as locally.
   Tickets, runs, aliases and settings are lost whenever it restarts. v2 depends on lasting state: the
   alias table (§5.3), versioned config (§8.2, §10) and graded history (§9). This must be fixed first.
   Options: DynamoDB Local with a disk volume (free, no AWS change), or real DynamoDB on-demand in
   eu-north-1 through an instance role (pennies a month, listed in CLAUDE.md §4).
5. **Booking authority is unstated.** D78: only the dashboard's Run now books real codes. §12 adds
   twice-daily schedules, and §10 has a single global `dry_run: true`. `common/booking_mode.py` forces
   dry-run when `HOOPSEDGE_ENV` is local, and books on `DRY_RUN=false` everywhere else, for every
   trigger. The brief must say whether scheduled runs ever book real codes.
6. **The kickoff cutoff loosens from 20 to 10 minutes.** CLAUDE.md invariant 2 uses
   `min_minutes_before_tip` 20; §7 uses `booking_cutoff_minutes` 10. Fine either way, but log it.
7. **The current matcher breaks §5.3.** `teams/matcher.py` auto-matches on rapidfuzz `token_set_ratio`
   ≥ 92. v2 bans fuzzy and substring matching, so fuzzy scores may only fill the review queue's
   suggestions.
8. **Code verification must not get weaker.** §2.4 asks for leg count and selections. v1 compares every
   selection's event, market, specifier and outcome (`sportybet/verification.py`). Keep v1's check.

### 3.3 Errors in §5.3 team matching

9. **Aliases are checked too late.** Step 4 runs after location plus mascot, so an alias added to
   correct a wrong step-3 match can never win. Check aliases first, then exact, then location plus mascot.
10. **`st → state` breaks Saint.** "St. Thomas (MN)" would become "state thomas". Only a trailing `st`
    means State; a leading `st` means Saint.
11. **Step 3 must require exactly one candidate.** Two ESPN teams that both satisfy "contains location
    and ends with mascot" must go to the queue.
12. **Aliases need a sport key.** The "Connecticut Huskies (football)" row shows that names repeat across
    sports, and ESPN team ids differ by sport.

### 3.4 Gaps

13. **Home and away orientation.** §4.6 uses ESPN's home team; markets 227/228 are SportyBet's home and
    away. v1 records `NORMAL`/`SWAPPED` per match. v2 must map team-total legs through it, or a swapped
    pairing books the wrong team.
14. **Team level has no source.** FBS/FCS/D1/other must come from somewhere. ESPN `groups` alone isn't
    enough, because an FBS-vs-FCS game appears in both feeds. Proposal: a conference-to-level table
    from ESPN's team and conference data, stored in the team registry.
15. **Provisional NCAAB/NBA tiers.** Proposal: the 33rd/67th percentiles of the current L5 PF and PA of
    same-level teams with `n ≥ min_games`, recomputed each run from games before the run.
16. **Tie-breaks.** Two `225` lines can be equally close to even (take the one nearer the ladder's middle,
    then the lower total). `proj == team_line` falls through to Under in §4.6; it should give no pick.
17. **Re-checking `safest` lines.** When the main line moves, the whole ladder moves. The booking-time
    re-check must re-select the main and booked lines from the fresh ladder and recompute both edges,
    not just compare one specifier.
18. **Postseason.** NCAAF bowls and conference championships, and NCAAB postseason, may use other
    tournament ids. They are out of scope until added to config.
19. **Grading needs main-line odds.** To compare line modes, every leg stores the main line's odds even
    when another line is booked.

### 3.5 Design concerns

20. **NCAAF timing.** The NCAAF regular season ends around 28–29 November 2026. With Phase 8's 3 dry-run
    days, NCAAF probably goes live after the season, and its grading buckets won't reach 100 legs this
    year. 🛑 Owner to choose: fast-track NCAAF, or build it properly for the 2027 season.
21. **§9 optimises the wrong thing.** Maximising hit rate under a minimum-legs rule always pushes the
    threshold up to that minimum. At `safest` odds near 1.40, a leg needs about 71% to break even.
    Proposal: judge thresholds on hit rate above the bookmaker-implied rate, and on flat-stake ROI.
22. **Too many calibration cells.** Sport × market × line mode × 5 edge buckets × cross-level × variant
    is 240 cells for NCAAB. Most never reach 100 legs. Proposal: suggest per sport × market × line mode,
    and show the other splits as read-only tables.
23. **The dashboard and API are a rework, not a shell.** API schemas, track record, Models and ticket
    views are built around probabilities, `unproven` labels and the model registry (D72, D76). v2 legs
    carry edges and lines. Plan for real changes in `api/` and most of `web/`.
24. **League rename.** The brief's `ncaab` replaces v1's `NCAA` (`League = Literal["NBA", "NCAA"]` in
    `engine/schemas.py`, about 20 files, and stored keys such as `SETTLEDLEG#NCAA`). Since state does
    not survive anyway (item 4), a clean rename costs code changes only, no data migration.

## 4. Keep, change, remove

| Area | v2 | Notes |
|---|---|---|
| `common/` | Keep | `booking_mode.py` changes with the answer to item 5 |
| `ingest/` | Change | Generalise the ESPN parser and backfill to football; incremental 2–3 day refresh; NCAAF tournament in the collector |
| `teams/` | Change | Level in the registry; matcher rewritten to §5.3 plus items 9–12; alias table persisted per sport |
| `ratings/`, `features/`, `models/`, `backtest/`, `learning/` | **Move to `legacy/`** | No active import path. `models/params/*.yaml` moves with them |
| `markets/probability.py` | Move to `legacy/` | v2 has no probability model |
| new `l5/` | New | Pure model: L5 inputs, projections, tiers, outlook, lean, team pick |
| `engine/` | Change | `build.py`, `config.py` and `schemas.py` import `models`. Rebuild around edges: kinds, cutoff, top-100 by \|edge\|, split by kickoff, rebook |
| `engine/recheck.py` | Change | Re-select main and booked lines from the fresh ladder (item 17) |
| `sportybet/` | Keep, extend | Add `sr:sport:16` and `sr:tournament:27653`; ladder parsing for `225` |
| `grading/` | Keep, extend | Grade booked and main lines; both `l5_same_level_only` variants |
| `pipeline/` | Change | `predict.py` uses the L5 model; `slate.py` stops importing `learning`; scheduler drops learning jobs and gets the §12 times |
| `api/` | Change | New config document, slate rows, CSV export, alias actions, calibration suggestions. `/models` goes |
| `web/` | Change | Sport switcher, slate sheet view, ticket panel, config page, review queue, calibration page |
| `deploy/` | Keep | Plus durable storage (item 4) |
| Docs | Change | `DECISIONS.md` entries from D82 on; CLAUDE.md gets a pointer to the brief |

## 5. Owner decisions (2026-11-22)

Logged as D82–D90 in `DECISIONS.md`.

| Question | Answer |
|---|---|
| Glossary Outlook rule (§2) | Confirmed as derived (D82) |
| Who books real codes (item 5) | Scheduled runs too, once config `dry_run` is false, which stays true until go-live sign-off (D83) |
| Durable storage (item 4) | DynamoDB Local on a disk volume, backed up off the instance (D84) |
| NCAAF this season (item 20) | Fast-track for the 2026-11-28 slate; supervised booking only (D85) |
| Kickoff cutoff (item 6) | 20 minutes (D86) |
| League rename (item 24) | `NCAA` → `NCAAB` (D87) |
| Brief §14 | Team totals 3.0 / 4.0; NBA 20-leg cap; same-level off, both graded; no Division II (D88) |

Still open: deleting `legacy/` for good, only on the owner's word. D89 and D90 need the owner's review.

## 6. Migration order

Each step is its own branch, left uncommitted for the owner to commit and merge before the next one.

### Track A: NCAAF fast-track (D85), target 2026-11-28

Built so it becomes the real v2 code, not a throwaway: the same modules Track B extends.

| Step | Branch | Work | Done when |
|---|---|---|---|
| A0 | `feature/v2-storage` | DynamoDB Local on a volume in both compose files, backup script, restart test. Relay spike on EC2: upcoming list for `sr:tournament:27653`, one event, dry-run payload, no booking | State survives a restart; spike result logged |
| A1 | `feature/v2-ncaaf-ingest` | ESPN parser and backfill for football (FBS `groups=80` and FCS `groups=81`), current-season backfill, 2–3 day incremental refresh, team registry with level from conference | NCAAF season backfilled; re-run fetches only recent days |
| A2 | `feature/v2-l5-model` | New pure `l5/` module: L5 inputs, projections, tiers, outlook (D82), lean, team pick, `l5_same_level_only`, cross-level flags. v2 config document with the brief's §10 values plus D86/D88. Golden test on all 46 rows. Grade the 2026-11-21 sheet against finals | Golden test green; first graded result reported |
| A3 | `feature/v2-ncaaf-slate` | Matcher rewrite (D89), alias table per sport with the §5.3 seeds, `225`/`227`/`228` ladder, main/safest/cushion line choice with tie-breaks, ticket kinds, 20-leg split by kickoff, 20-minute cutoff, started-game guard, recheck from a fresh ladder, CSV export from the command line, dry-run tickets | §11.2–6 green; a dry-run CSV and tickets for a live NCAAF slate; supervised booking possible on the owner's word |

### Track B: the rest of v2

| Step | Branch | Work | Done when |
|---|---|---|---|
| B1 | `feature/v2-legacy` | `ratings/`, `features/`, `models/`, `backtest/`, `learning/`, `markets/` to `legacy/` with no active import path; `NCAA` → `NCAAB` (D87); engine, pipeline and scheduler on the L5 model | Tests green; no active module imports `legacy/` (a test enforces it) |
| B2 | `feature/v2-basketball` | NCAAB and NBA on the L5 model and incremental ingest; provisional percentile tiers; schedule at §12 times | Dry-run tickets for live NCAAB and NBA slates |
| B3 | `feature/v2-booking` | D83 booking authority in `booking_mode.py`, verification kept at v1 strength, rebook (remove started legs, re-check, new code, linked tickets) | Booking tests green; rebook works in dry-run |
| B4 | `feature/v2-api` | Config document API (versioned), slate rows, CSV export, alias actions, review queue, relay health; `/models` removed | API tests green; OpenAPI regenerated |
| B5 | `feature/v2-dashboard` | Sport switcher, slate sheet view with flags, ticket panel (odds, bookmaker-implied probability, countdown, copy, rebook), config page, review queue | Owner produces the CSV and sees tickets for all three sports |
| B6 | `feature/v2-grading` | Grade booked and main lines and both variants; calibration report and suggestions (D90); weekly evidence report | First week of graded legs reported |
| B7 | — | 3 dry-run days with no CRITICAL alerts, one supervised live booking per sport, then `dry_run: false` | Owner signs off |
