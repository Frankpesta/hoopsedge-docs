# HoopsEdge v2 — Rebuild Brief for Claude Code

> **Read this whole file before touching code.** It supersedes `CLAUDE.md` and `docs/*` wherever they conflict. Where this brief is silent, the existing docs still apply (cost rules, no-placing-bets, dry-run default, verified codes, loud failures).
>
> Work in the order of §12. Stop and ask the owner at every 🛑 checkpoint.

---

## 1. What changes and why

HoopsEdge v1 was designed around a trained model (ratings, LightGBM, joint score distributions, backtests). v2 replaces that prediction core with a **simple, transparent last-5-games model**. The owner has already run this model by hand on live slates and wants it productionised. v2 also **adds NCAA Football (NCAAF)** alongside basketball.

| Area | v1 | v2 |
|---|---|---|
| Prediction core | Ratings + LightGBM + score distributions | **L5 average model** (§4), pure and deterministic |
| Sports | NBA, NCAA men's basketball | **NBA, NCAAB, NCAAF** |
| Line choice | Main line | **Configurable per market**: `main` or `safest` (§6) |
| Markets | Handicap, FT O/U, HT O/U, team O/U | **v2 scope: FT O/U and team O/U.** Handicap and HT O/U stay disabled until the owner re-enables them |
| "Learning" | Model retraining | **Grading and threshold calibration** (§9). No ML training |
| Output | Dashboard, tickets, codes | Same, plus a **CSV/sheet export in the owner's format** (§8) |

**Keep:** infrastructure, SportyBet client, ticket and booking engine, grading, dashboard shell, ops tooling, invariants.
**Remove from the active pipeline:** ratings, feature store, training, backtest harness, model registry. Don't delete them yet: move them to `legacy/` behind a dead import path, and record that in `DECISIONS.md`. 🛑 Ask the owner before deleting `legacy/` for good.

### First task: audit

Before any change, write `docs/reports/v2-audit.md`. List what exists in the repo today (which phases from v1 are actually built), what v2 keeps, changes and removes, and the migration order. 🛑 The owner reviews it before you start.

---

## 2. Non-negotiable invariants (carried over, plus v2 additions)

1. **Never place a bet, never touch money.** Only book codes (share codes). There is no stake endpoint anywhere in the code.
2. **Never book a leg on a game that has started.** Re-check `matchStatus === "Not start"` and `estimateStartTime > now` immediately before booking. A game that started between prediction and booking is dropped and logged.
3. **Fresh lines only.** Read lines from SportyBet at most **5 minutes** before booking. If a line has moved, recompute the edge with the new line; if the leg no longer clears its threshold, drop it.
4. **Verify every code.** After booking, fetch the code back and assert that the leg count and selections match. If not, mark it failed and alert.
5. **No guessed team matches.** Unmatched or ambiguous teams go to a review queue and the game is skipped (§5.3). v2 has already seen false positives in practice (e.g. "Florida Tech Panthers" fuzzy-matched to FIU).
6. **Current season only** for L5 inputs.
7. **Deterministic and pure model.** Same inputs give the same output. All thresholds come from config (§10), never hard-coded.
8. **Idempotent runs** with the existing locking and `run_id` scheme.
9. **Dry-run by default.** `DRY_RUN=true` produces everything except the POST to the booking endpoint.

---

## 3. Sports and sources

| Sport | Results source (ESPN public JSON) | SportyBet sportId / tournament |
|---|---|---|
| NCAAF | `site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard?dates=YYYYMMDD&groups=80&limit=500` (FBS). Also pull `groups=81` (FCS) so FBS-vs-FCS opponents have data | `sr:sport:16`, NCAA Regular Season `sr:tournament:27653` (Div II championship and others are out of scope) |
| NCAAB | `.../basketball/mens-college-basketball/scoreboard?dates=YYYYMMDD&groups=50&limit=500` (D1) | `sr:sport:2`, NCAA Regular Season `sr:tournament:648` |
| NBA | `.../basketball/nba/scoreboard?dates=YYYYMMDD` | `sr:sport:2`, NBA `sr:tournament:132` |

Notes from live runs:
- ESPN scoreboard returns `events[].competitions[0].competitors[]` with `team.displayName`, `team.location`, `team.shortDisplayName`, `team.abbreviation`, `score`, `homeAway`, and `status.type.completed`. Use the ESPN team `id` as the canonical key, not names.
- **Ingest incrementally.** Backfill the current season once (day by day from season start), then pull only the last 2–3 days on each run. A full backfill of NCAAB from season start was about 1,100 games and 23 requests: cheap, but don't repeat it on every run.
- **About 30% of SportyBet's "NCAA, Regular Season" basketball list is Division II.** ESPN D1 data doesn't cover those teams. They must fail matching cleanly and be skipped, not mismatched.
- **ESPN occasionally includes D2/NAIA teams that played a D1 opponent.** Those teams have only 1–2 games, so the minimum-games rule (§4.3) excludes them.

---

## 4. The model (this is the core; implement exactly)

### 4.1 Inputs per team

From the team's **last N completed games of the current season** (default `N = 5`, sorted by date, most recent first):
- `PF` = mean points scored
- `PA` = mean points allowed
- `n` = number of games used (≤ N)

### 4.2 Outputs per game

```
home_proj   = (home.PF + away.PA) / 2
away_proj   = (away.PF + home.PA) / 2
model_total = home_proj + away_proj
sum_of_avgs = away.PF + home.PF          # naive baseline, informational only
```

### 4.3 Eligibility

- Skip a game if either team has `n < min_games` (default 4).
- Skip if either team is unmatched (§5.3).
- **Flag** (don't skip) cross-level games: FBS vs FCS in NCAAF, or D1 vs non-D1 opponents inside a team's L5. Show a ⚠ in the UI and the export. This was the single biggest source of bogus edges in live runs:
  - Chattanooga–Alabama came out at −22.7 because Chattanooga's L5 was all FCS games.
  - Early-season NCAAB overs were inflated by 100-point wins over non-D1 teams.
- Config flag `l5_same_level_only` (default **false**, to match the owner's hand-run model): when true, the L5 window only counts games against opponents of the same level (FBS/D1). Build it, test it, and report its effect in the grading stats (§9) so the owner can decide to turn it on.

### 4.4 Tiers (informational columns, configurable)

Thresholds derived from the owner's NCAAF sheet. Expose them in config, per sport:

| Sport | Attack tier (from PF) | Defence tier (from PA, lower is better) |
|---|---|---|
| NCAAF | Low < 24.0 ≤ Medium < 33.0 ≤ High | High ≤ 20.0 < Medium < 28.0 ≤ Low |
| NCAAB / NBA | Provisional: 33rd/67th percentile of current-season team PF/PA in that league. Mark as `provisional` in the UI | same |

**Glossary Outlook (LOW/MEDIUM/HIGH).** This is a points score built from the four tiers, and it reproduces all 46 rows of the owner's sheet:

```
atk_pts: Low=0, Medium=1, High=2        (applied to both teams' attack tiers)
def_pts: High=0, Medium=1, Low=2        (applied to both teams' defence tiers; weak defence = more points)
score   = atk_pts(away) + atk_pts(home) + def_pts(away) + def_pts(home)     # range 0..8
outlook = LOW if score <= 2, MEDIUM if 3 <= score <= 5, HIGH if score >= 6
```

Keep the band edges (2 / 5) in config. Because the outlook is built from the tiers, it inherits their `provisional` label in NCAAB and NBA. **The outlook does not affect the lean.**

### 4.5 Lean

```
edge = model_total - line          # line = the line chosen per §6 for "lean" purposes (the MAIN line)
OVER  if edge >=  over_threshold[sport]
UNDER if edge <= -under_threshold[sport]
PASS  otherwise
```

Defaults reproduce the owner's sheet exactly on all 46 NCAAF rows (see the golden test, §11):

| Sport | over_threshold | under_threshold |
|---|---|---|
| NCAAF | 4.0 | 5.0 |
| NCAAB | 5.0 | 5.0 |
| NBA | 5.0 | 5.0 (provisional) |

**The lean is always decided against the MAIN line.** The line actually booked can be different (§6). Store both, plus the edge against each.

### 4.6 Team-total pick (per game)

For each side with an available team-total market:

```
side_edge = proj - team_line
direction = Over if proj > team_line else Under
```

Pick the side with the larger `|side_edge|`. Apply `team_total_threshold[sport]` (default NCAAF 3.0, NCAAB 4.0, NBA 4.0); below it, no team-total leg for that game. In the hand-run version there was no threshold, so every game got a pick. The owner should see that the threshold exists and be able to set it to 0.

---

## 5. SportyBet integration

### 5.1 Endpoints (observed working, `ng` locale)

| Purpose | Request |
|---|---|
| Upcoming events | `GET /api/ng/factsCenter/pcUpcomingEvents?sportId={sportId}&marketId=1,18,10&pageSize=100&pageNum={n}&todayGames=false&timeline=&tournamentId={tournamentId}` → `data.tournaments[].events[]`, `data.totalNum`; paginate until all events are fetched |
| Event detail (all markets) | `GET /api/ng/factsCenter/event?eventId={sr:match:…}&productId=3` → `data.markets[]`, `data.matchStatus`, `data.estimateStartTime` (epoch ms) |
| Book code | `POST /api/ng/orders/share` with body `{"selections":[{"eventId","marketId","specifier","outcomeId"}]}` → `data.shareCode` |
| Verify code | `GET /api/ng/orders/share/{code}` → `data.ticket.selections[]` |

Notes from live runs:
- `eventId` is the Sportradar id (`sr:match:NNNN`). ESPN doesn't expose it, so match by team names and kickoff time (§5.3).
- Event detail has no `marketId` filter; take everything, then filter.
- SportyBet may geo-restrict. Keep the existing optional NG-proxy SSM parameter. Server-side HTTP has not been tested from AWS: test it in the first spike.
- In a browser context, the site overrides `window.fetch`. That doesn't matter server-side; just don't copy browser snippets blindly.

### 5.2 Market and outcome mapping

| Market | id | specifier | outcome desc |
|---|---|---|---|
| Total incl. OT (game O/U) | `225` | `total=49.5` | `"Over 49.5"` / `"Under 49.5"` |
| Home team total incl. OT | `227` | `total=37.5` | `"Over …"` / `"Under …"` |
| Away team total incl. OT | `228` | `total=13.5` | `"Over …"` / `"Under …"` |
| 1st-half O/U (disabled in v2) | `68` | `total=27.5` | |
| Handicap incl. OT (disabled in v2) | `223` | `hcp=-25.5` | |
| Winner incl. OT | `219` | — | `Home` / `Away` |

- Match outcomes by the `desc` prefix (`startsWith("Over")` or `startsWith("Under")`), then use that outcome's `id` (observed: Over = `12`, Under = `13`, but don't hard-code them).
- Only use markets with `status === 0` and outcomes with `isActive === 1`.
- **Multiple `225` entries are the alternate lines.** Depth differs by sport:
  - NCAAF: about 5 lines (main ±2)
  - NCAAB: about 15 lines (main ±7)
- Team totals had a **single line** per side in every game observed.

### 5.3 Team matching (SportyBet name → ESPN team id)

1. Normalise: lowercase, strip accents, fix mojibake (e.g. `FightinÂ´` → `fightin`), drop punctuation, `st` → `state`, collapse spaces.
2. Try an exact match against ESPN `displayName`.
3. Then try **location + mascot**: the SportyBet name must contain ESPN's location **and** end with ESPN's mascot (mascot = `displayName` minus `location`).
4. Then check a persisted **alias table** (`team_aliases`, editable in the dashboard). Seed it with these, observed live:

| SportyBet | ESPN |
|---|---|
| Texas Rio Grande Valley Vaqueros | UT Rio Grande Valley Vaqueros |
| University At Albany Great Danes | UAlbany Great Danes |
| Penn Quakers | Pennsylvania Quakers |
| New Orleans Privateers | (ESPN displayName as returned, e.g. "LSU New Orleans Privateers"; confirm by team id) |
| Lafayette College Leopards | Lafayette Leopards |
| Middle Tennessee State Blue Raiders | Middle Tennessee Blue Raiders |
| St. Thomas (MN) Tommies | St. Thomas Tommies |
| Wisconsin Milwaukee Panthers | Milwaukee Panthers |
| Nicholls State Colonels | Nicholls Colonels |
| Morehead St Eagles | Morehead State Eagles |
| Connecticut Huskies (football) | UConn Huskies |
| Delaware Fightin´ Blue Hens | Delaware Blue Hens |

5. Anything still ambiguous or unmatched goes to the `UNMATCHED` review queue and the game is skipped. **Never** fall back to a first-word or substring match. Those produced wrong matches in testing (Florida Tech → FIU, Georgia Bobcats → Georgia, Minnesota State Mankato → Minnesota, St Thomas Aquinas → St. Thomas).
6. Cross-check kickoff time: the SportyBet `estimateStartTime` must be within ±3 h of the ESPN scheduled time for the same pairing. If it isn't, don't match.

---

## 6. Line selection

There are two separate decisions: **which way to lean** (always against the main line, §4.5) and **which line to book**.

- **Main line**: the `225` entry whose Over and Under odds are closest to each other.
- **Line modes** (config, per sport and per market, overridable per run from the dashboard):
  - `main`: book the main line.
  - `safest` (the owner's preferred mode): for an **Over**, book the **lowest** available line; for an **Under**, book the **highest** available line. This gives the lowest odds and the highest hit probability.
  - `safest_with_cushion`: like `safest`, but only if `model_total - booked_line >= min_cushion` for Overs (mirror for Unders). Default `min_cushion = 0`. This is for the owner's later tuning.
- Team totals: single line, so the mode doesn't apply.
- Record for every leg: main line, booked line, edge vs main, edge vs booked, odds.

Observed odds ranges, to sanity-check the UI:

| Sport | `main` odds | `safest` odds |
|---|---|---|
| NCAAF | ~1.85 | ~1.63–1.75 |
| NCAAB | ~1.81–1.87 | ~1.39–1.43 |

---

## 7. Tickets and booking

- **Ticket kinds per sport per slate:**
  1. `TOTALS_LEAN`: only games with an OVER/UNDER lean, using the configured line mode.
  2. `TEAM_TOTALS`: one team-total leg per eligible game (§4.6).
  3. `TOTALS_ALL_OVER` / `TOTALS_ALL_UNDER` (off by default): every eligible game on the same side using `safest` lines. The owner asked for these by hand. Show a "model disagrees" count for these tickets: legs where the edge against the booked line is negative.
- **Max legs per ticket**: 20 (config). Split larger sets into multiple tickets, ordered by kickoff, so the earliest games are in the first ticket. NCAAB can exceed 100 games a day: cap at the top 100 by |edge| per ticket kind, as in v1.
- Drop legs below threshold. Never pad tickets.
- Each ticket gets its own booking code. Show the **total odds** (product of leg odds, 2 dp), leg count, earliest kickoff, and a naive hit probability (product of implied probabilities, labelled "bookmaker-implied").
- **Kickoff cutoff**: don't book a leg that starts within `booking_cutoff_minutes` (default 10). Show a countdown on the dashboard for the earliest leg.
- Also store the selections so a ticket can be **rebooked** with fresh lines after a game starts (rebook = remove started legs, re-check lines, book a new code, link the two tickets).

---

## 8. Outputs

### 8.1 Slate export (CSV, and a sheet view in the dashboard)

Same columns as the owner's sheet, plus v2 extras at the end:

```
Away,Home,Away Atk/Def,Home Atk/Def,Away L5 PF/PA,Home L5 PF/PA,Sum of Avgs,Model Total,Glossary Outlook,Vegas Total,Lean,
Kickoff (WAT),Main Line,Booked Line,Booked Odds,Edge vs Main,Edge vs Booked,Home Proj,Away Proj,Team Pick,Team Line,Team Odds,Flags
```

- "Vegas Total" = SportyBet main line (keep the column name for continuity with the sheet).
- Atk/Def as `High/Low`; L5 values as `26.8/23.6` with 1 dp.
- **All times in Africa/Lagos (WAT).** Store UTC; display WAT.
- File name: `{sport}_predictions_{YYYYMMDD}.csv`.

### 8.2 Dashboard additions

- Sport switcher: NCAAF / NCAAB / NBA.
- Slate table: the export columns, sortable; flags shown as badges; started games greyed out.
- Ticket panel: code, legs, total odds, earliest kickoff countdown, copy button, rebook button.
- Config page: thresholds, line modes, `min_games`, `l5_same_level_only`, max legs, cutoff minutes. Every change is versioned (v1 config scheme).
- Review queue: unmatched teams with an "add alias" action.

---

## 9. Grading and calibration (replaces "learning")

- After each game finishes, grade every stored leg: won, lost, or push. Use the booked line and the main line, so you know how the other line choice would have done too.
- Aggregate hit rate and flat-stake ROI by: sport × market × line mode × edge bucket (e.g. 0–2, 2–4, 4–6, 6–10, 10+) × flags (cross-level yes/no) × `l5_same_level_only` (compute both variants for every game, even though only one is active).
- **Threshold suggestions, not auto-changes.** Once a bucket has ≥ 100 graded legs, propose new thresholds that maximise hit rate subject to a minimum leg count per slate. Show them on the dashboard with the supporting numbers. The owner clicks to apply them, which creates a new config version. 🛑 Nothing changes thresholds on its own.
- Weekly evidence report (keep the v1 report location) with the tables above.

---

## 10. Config (single versioned document)

```yaml
sports:
  ncaaf:
    enabled: true
    tournament_ids: ["sr:tournament:27653"]
    espn: { path: "football/college-football", groups: [80, 81] }
    l5_window: 5
    min_games: 4
    l5_same_level_only: false
    over_threshold: 4.0
    under_threshold: 5.0
    team_total_threshold: 3.0
    tiers: { atk: [24.0, 33.0], def: [20.0, 28.0] }
    line_mode: { totals: safest, team_totals: main }
  ncaab:
    enabled: true
    tournament_ids: ["sr:tournament:648"]
    espn: { path: "basketball/mens-college-basketball", groups: [50] }
    l5_window: 5
    min_games: 4
    l5_same_level_only: false
    over_threshold: 5.0
    under_threshold: 5.0
    team_total_threshold: 4.0
    tiers: provisional_percentiles
    line_mode: { totals: safest, team_totals: main }
  nba:
    enabled: true
    tournament_ids: ["sr:tournament:132"]
    espn: { path: "basketball/nba" }
    l5_window: 5
    min_games: 4
    over_threshold: 5.0
    under_threshold: 5.0
    team_total_threshold: 4.0
    tiers: provisional_percentiles
    line_mode: { totals: safest, team_totals: main }
tickets:
  max_legs: 20
  max_games_per_kind: 100
  booking_cutoff_minutes: 10
  kinds: { TOTALS_LEAN: true, TEAM_TOTALS: true, TOTALS_ALL_OVER: false, TOTALS_ALL_UNDER: false }
timezone_display: Africa/Lagos
dry_run: true
```

---

## 11. Tests (must exist before the first live booking)

1. **Golden test: NCAAF sheet.** Fixture `tests/fixtures/ncaaf_predictions_20261121.csv` (the owner's file: 46 rows, columns as in §8.1, first 11 columns). For every row, recompute from the L5 PF/PA columns and assert:
   - `Model Total` matches to 0.1. Examples: BYU@Kansas 26.8/23.6, 26.8/25.6 → 51.4; Wake Forest@Georgia Tech 51.2/7.0, 19.8/33.4 → 55.7; Ohio State@Nebraska → 42.2; Chattanooga@Alabama → 35.8; Colorado St@Fresno St → 70.2.
   - `Sum of Avgs` matches.
   - `Lean` matches on **all 46 rows** with thresholds 4.0 / 5.0. Edge cases that pin the thresholds:
     - Colorado St +3.7 → PASS
     - FAU +3.8 → PASS
     - Colorado +4.3 → OVER
     - BYU +4.9 → OVER
     - South Alabama −4.8 → PASS
     - Houston −6.8 → UNDER
   - `Glossary Outlook` matches on all 46 rows using the §4.4 tier score, computed from the sheet's own Atk/Def columns.
   - Atk/Def tiers match with §4.4 thresholds. Where one doesn't, list it in the test report rather than tweaking thresholds to force it; it's a 🛑 question for the owner.
2. **Line selection:** given a fixture event JSON with 5 or 15 `225` lines, `main`, `safest`-Over and `safest`-Under pick the right lines and outcome ids.
3. **Team matching:**
   - Every alias in §5.3 resolves.
   - The known false-positive pairs (Florida Tech, Georgia Bobcats, Minnesota State Mankato, St Thomas Aquinas) **don't** match.
   - D2 names fall through to the review queue.
4. **Started-game guard:** a leg whose `matchStatus` flips to live between predict and book is dropped.
5. **Ticket splitting:** 47 legs → 20/20/7, ordered by kickoff.
6. **Booking verification:** a mocked share response with a wrong leg count fails loudly.
7. **Cost assertion test** from v1 still passes.

---

## 12. Build order

| Phase | Work | Done when |
|---|---|---|
| 0 | Audit (§1) and a SportyBet server-side spike: fetch the upcoming list, one event and a dry-run payload from AWS, with and without the proxy | Audit reviewed 🛑; spike result in `DECISIONS.md` |
| 1 | Config v2 schema and migration; move v1 model code to `legacy/` | Config editable in the dashboard; pipeline still runs with stubs |
| 2 | ESPN ingest for NCAAF (new), NCAAB and NBA (reuse); team registry with level (FBS/FCS/D1/other) | Current-season backfill complete; incremental updates work |
| 3 | L5 model (§4) as a pure module, plus the golden test | Golden test green |
| 4 | SportyBet event fetch, team matching and alias table, line selection | Matching and line tests green; review queue visible |
| 5 | Ticket builder, booking, verification, rebook | Dry-run tickets generated for a live slate; tests green |
| 6 | Exports and dashboard updates (§8) | Owner can produce the CSV and see tickets for all three sports |
| 7 | Grading and calibration reports (§9) | First week of graded legs reported |
| 8 | Go-live: 3 days of dry runs across all sports with no CRITICAL alerts, then one supervised live booking per sport | Owner signs off 🛑 |

Pipeline schedule (WAT, configurable):
- **NCAAF:** Saturday 14:00 and 18:00; weeknight slates at 18:00 on game days.
- **NCAAB / NBA:** daily at 16:00 and 21:00 (the second run catches late lines), plus an on-demand "run now" button.

---

## 13. Things the owner should know (surface them in the UI, not just in docs)

- Each leg is close to a coin flip at `main` odds. Long accumulators very rarely land. The ticket panel shows the bookmaker-implied hit probability next to the total odds.
- Early-season L5 numbers are noisy and inflated by mismatched opponents. Cross-level flags are shown in the slate table and the export.
- `safest` lines lower the odds sharply (NCAAB ≈ 1.40 per leg), but the leg still loses if the game goes the other way by enough. The model's cushion against the booked line is shown per leg.

## 14. Open questions for the owner 🛑

1. Confirm the Glossary Outlook tier-score rule (§4.4). It was reverse-engineered and matches all 46 rows of the sheet.
2. Should `l5_same_level_only` start on or off? The default is off, to match your hand-run sheet.
3. Default team-total thresholds: keep 3.0 / 4.0, or 0 (always pick) as in the hand run?
4. Do NBA tickets use the same 20-leg cap, or one ticket per market as in v1?
5. Should NCAAF include Division II games? (v2 excludes them: no ESPN data.)
