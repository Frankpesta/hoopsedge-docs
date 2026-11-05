# Phase 5 — Market Probabilities, Leg Selection & Ticket Builder

**Goal:** pure, deterministic functions that turn predictions and SportyBet offers into tickets. No network, no AWS. 100% unit-testable.

## 5.1 Inputs

- `Prediction`: `{game_id, league, model_version, feature_hash, ft: {mu_h, mu_a, sd_margin, sd_total, rho}, h1: {...}, confidence: {gp_h, gp_a, prior_w_h, prior_w_a}}`
- `Offer` (normalized from SportyBet by doc 07):
  - `{event_id, game_id, market_family, market_id, specifier, line, outcomes: [{outcome_id, side, odds}], is_main, tip_time, status}`
  - `side` ∈ `home|away|over|under` and, for team totals, `{team: home|away, dir: over|under}`

## 5.2 Market probability (`markets/`)

All probabilities come from the bivariate normal of the relevant period. `margin = home − away`, `total = home + away`.

| Family | Outcome | P(win) | P(push) |
|--------|---------|--------|---------|
| handicap, line L on home (home −5.5 → L=−5.5) | home | P(margin + L > 0) | P(margin + L = 0) if L integer |
| | away | P(margin + L < 0) | same push |
| total_ft / total_1h, line T | over | P(total > T) | P(total = T) if T integer |
| | under | P(total < T) | |
| team_total, team t, line T | over/under | on that team's marginal | |

Then apply the league × family calibrator to P(win).

For the push-aware win probability, use `p_eff = P(win) / (1 − P(push))` only for **ranking**. Store all three: `p_win`, `p_push`, `p_lose`.

**Fair (no-vig) implied probability:** for a two-way market with decimal odds o₁, o₂, `q_i = (1/o_i) / (1/o₁ + 1/o₂)`.
- **Edge** = `p_win_calibrated − q`.
- **EV per unit** = `p_win·(o − 1) − p_lose`.

## 5.3 Main line (D5)

When SportyBet offers several specifiers for a family on one event, the main line is:
1. the specifier SportyBet flags as main, if Phase 0 found such a flag; otherwise
2. the specifier minimizing `|q_home − 0.5|` (or `|q_over − 0.5|`).

If `allow_alt_lines=false`, only the main line is scored.

## 5.4 Leg eligibility (all must pass; every rejection is recorded with a reason code)

| Code | Rule |
|------|------|
| `UNMATCHED` | event not matched to a canonical game (invariant 5) |
| `STARTED` / `TOO_CLOSE` | event not `not_started`, or tip < now + `min_minutes_before_tip` (invariant 2) |
| `MARKET_DISABLED` | family disabled in config or gated by the evidence report |
| `LOW_PROB` | `p_win_calibrated < min_prob` |
| `LOW_EDGE` | `edge < min_edge` |
| `LOW_CONFIDENCE` | either team's games played < `min_team_games_for_full_confidence` **and** the prior weight > 0.6. The leg is still allowed when prob ≥ `min_prob + 0.04`, which is a stricter bar for thin data |
| `MISSING_PRED` | no prediction for the period (e.g. no 1H model) |
| `STALE_ODDS` | offer snapshot older than 5 min at booking time (invariant 3) |
| `NBA_INJURY_UNCERTAIN` | NBA: a starter is listed Questionable and the latest injury snapshot is older than 3 h before tip (configurable) |

## 5.5 Selection & partitioning (per league × market family)

```
legs = eligible legs
per game: keep only the single best outcome by p_eff, tie-break edge   # D10 (team_total: best of 4)
rank by p_eff desc, then edge desc, then event_id (deterministic)
NBA:  take top max_legs_per_ticket → ticket 1
NCAA: take top max_legs_per_market (100) → chunk into tickets of 20 in rank order
      (ticket 1 = ranks 1–20, ticket 2 = 21–40, …)
drop any ticket with < min_legs legs
```

For every ticket, compute:
- `combined_odds = Π o_i` (pushes and voids settle at 1.0)
- `combined_prob = Π p_win_i`, assuming independence across games (record this as an assumption)
- `expected_value = combined_prob·combined_odds − 1`

The dashboard shows `combined_prob` exactly, e.g. "1 in 4,200".

## 5.6 Determinism & tests

Same inputs give the same tickets; this is a property test with shuffled input order.

Unit tests must cover:
- Push math for integer lines.
- The no-vig computation.
- Same-game uniqueness per ticket (invariant D10).
- No leg in two tickets of the same market.
- Partitioning edge cases with 0, 1, 19, 20, 21, 99, 100, 101 and 250 eligible legs.
- Short last ticket dropped below `min_legs`.
- Every rejection carries a reason code.

## Definition of Done

- [ ] `engine.build_tickets(predictions, offers, config, now)` implemented and fully covered by tests.
- [ ] Golden-file test: a recorded real slate (fixtures) produces a stable ticket JSON.
