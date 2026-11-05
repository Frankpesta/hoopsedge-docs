# Phase 0 — Feasibility Spikes

**Goal:** prove every external dependency works *before* building on it, and record real fixtures. No AWS infrastructure is built in this phase. Everything runs from the owner's machine with `scripts/`.

Nothing in this doc about third-party endpoints is guaranteed. The endpoints below are **candidates to verify**. Record what is actually true in `docs/DECISIONS.md` and `services/hoopsedge/sportybet/markets.yaml`.

## 0.1 SportyBet discovery (`scripts/sportybet_discovery.py`)

Use the browser DevTools Network tab on `https://www.sportybet.com/ng/sport/basketball` and a booking flow ("Book a Bet" without logging in) to identify:

| What | Candidate | Record |
|------|-----------|--------|
| Upcoming basketball events | `GET /api/ng/factsCenter/pcUpcomingEvents?sportId=sr:sport:2&...` | URL, params, pagination, response schema |
| Event detail with all markets | `GET /api/ng/factsCenter/event?eventId=...` (or similar) | which call returns alternate markets/specifiers |
| Create booking code | `POST /api/ng/orders/share` with `{selections:[{eventId, marketId, specifier, outcomeId}]}` | exact body, required headers/cookies, response field holding the code |
| Decode booking code | `GET /api/ng/orders/share/{code}` | response schema for verification |

Discover and record in `markets.yaml`:

- Tournament/category IDs for **NBA** and **NCAA** (NCAA may be split across several tournaments).
- For each market family, the following, and whether the market includes overtime:
  - `marketId`
  - specifier format (e.g. `hcp=-5.5`, `total=221.5`)
  - outcome IDs and their meaning

  | Family | Expected name on site |
  |--------|----------------------|
  | `handicap` | "Handicap (incl. overtime)" |
  | `total_ft` | "Over/Under (incl. overtime)" |
  | `total_1h` | "1st half - Over/Under" |
  | `team_total` | "Home/Away team Over/Under" (may be two separate markets) |

- How the "main line" is identified. Is there a flag, or is it the specifier with the most balanced prices?
- **Maximum selections per booking**, and whether two selections from the same event are rejected.
- Whether booking requires any token/cookie obtained from a prior page load.
- Event status fields: how "not started", "live", "suspended" and "postponed" appear.

Save at least 3 complete raw responses of each type to `services/tests/fixtures/sportybet/` (scrub any cookies). These recorded fixtures are what all later tests use.

**Booking-test etiquette:** create no more than 5 test booking codes in total during discovery. Do not log in. Booking codes are not bets.

## 0.2 Geo / anti-bot test

1. Run the events call and a booking call from the owner's machine (Nigeria).
2. Run the same calls from AWS: a throwaway Lambda in `us-east-1`, deployed manually and deleted afterwards. If blocked, also try `af-south-1` and `eu-west-1`.
3. Record the outcome: status codes, Cloudflare challenge pages, latency.

Decision table:

| Result | Action |
|--------|--------|
| AWS works directly | `sportybet_proxy_enabled=false`. Done. |
| AWS blocked, a different region works | Deploy the SportyBet functions to that region only (justify in DECISIONS). |
| All AWS regions blocked by IP/geo | Use a Nigerian residential/ISP proxy for the SportyBet client only. **Ask the owner** to choose and pay for one; store the URL in SSM. |
| Blocked by a JS challenge, not by IP | Stop and report to the owner. Options: a Playwright headless browser on Fargate Spot (costlier), or booking from the owner's machine via a small local agent. Do not pick one without approval. |

## 0.3 Data-source proof (`scripts/espn_probe.py`)

ESPN public JSON endpoints (candidates, verify):

- NBA scoreboard: `https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates=YYYYMMDD`
- NCAA scoreboard: `https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/scoreboard?dates=YYYYMMDD&groups=50&limit=400`
  (`groups=50` should return all D1 games. Verify that the count matches a known busy day.)
- Game summary: `.../summary?event={id}`. This should return the box score, linescores (half scores for NCAA, quarters for NBA), venue and the neutral-site flag.
- NBA injuries: `https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries`

Prove each of the following and record it:

- [ ] Half/period scores are present for **≥ 99%** of completed games in a sampled past season, for both leagues.
- [ ] Box scores contain FGA, FGM, 3PA, 3PM, FTA, FTM, OREB, DREB, TOV and PTS per team.
- [ ] NBA box scores contain player minutes (needed for injury impact).
- [ ] Rate behaviour: 2 requests per second is sustainable without 429s.
- [ ] Historical depth: seasons available. Target **2015-16 onward** for NBA and **2015-16 onward** for NCAA (COVID 2019-20 and 2020-21 are flagged, not dropped).

If ESPN is insufficient for NCAA, the fallback is CollegeBasketballData.com (free API key, ask the owner to register). Record which source wins in DECISIONS.

## 0.4 Historical lines (for evidence against the market)

The model's accuracy can be measured against final scores alone, but **edge** needs historical market lines. Investigate, in order:

1. Whether ESPN summaries include a `pickcenter`/odds block for past games (often present for recent seasons).
2. Free public archives of historical closing lines.
3. If none is adequate: evidence against the market starts **from day one of live logging**. Every SportyBet offer snapshot is stored for that purpose.

Record the available coverage per league and season.

## Definition of Done

- [ ] `markets.yaml` filled with verified IDs for all four market families, both leagues.
- [ ] Recorded fixtures for events, event detail, booking response and code decode.
- [ ] Geo decision recorded and actioned.
- [ ] ESPN coverage report (`docs/reports/phase0-data-coverage.md`) committed.
- [ ] Historical-lines availability recorded.
- [ ] Owner has reviewed the DECISIONS entries from this phase.
