# Phase 3 — Point-in-Time Ratings & Features

**Goal:** for any game G, produce the exact feature vector that was knowable just before G tipped. This module is used both for training (replaying every historical game) and for live prediction (today's games). The two paths must run **the same code**.

## 3.1 The core API

```python
def ratings_as_of(league: str, season: str, as_of: datetime) -> RatingsSnapshot: ...
def features_for_game(game: GameRef, as_of: datetime) -> FeatureRow: ...
```

`as_of` is always the game's tip time, or *now* for live use. Every internal query filters `tip_time < as_of`.

## 3.2 Ratings (recomputed after every completed game, so the model learns from every game)

**A. Opponent-adjusted efficiency and tempo (primary).**
- Use the possession-level box totals of every current-season game before `as_of`.
- Fit ridge regressions:
  - Points per 100 possessions: `ppp_ij = μ + O_i − D_j + h·home_i + ε`.
  - Tempo: `poss_ij = τ + T_i + T_j + ε`.
- **Recency weighting:** an exponential half-life (default NBA 30 days, NCAA 40 days). Tuned in Phase 4.
- **Shrinkage toward the preseason prior (D1):** implement as a ridge penalty toward the prior rather than toward 0. A team with *n* games gets prior weight `k/(k+n)`, where `k` is tuned (start NBA 8, NCAA 6).
- **Preseason prior:** last season's end-of-season rating regressed toward the league mean by `r` (start NBA 0.35, NCAA 0.45). Teams with no previous season (new to D1) get the mean of the bottom D1 quartile.
- Also compute the **four factors** (eFG%, TOV%, ORB%, FTr) and **3PA rate**, offence and defence, opponent-adjusted the same way.
- First-half ratings: the same fit on 1st-half points and 1st-half possessions (estimated as 1H points share × possessions if 1H possessions aren't available).

**B. Margin-aware Elo (secondary, a robustness signal).**
- Season starts from the regressed prior.
- K tuned per league.
- MOV multiplier, autocorrelation-adjusted (FiveThirtyEight-style).
- Home advantage parameter.

**C. Home-court advantage**, re-estimated as a league-wide parameter each day (NCAA HCA has drifted downward over recent seasons). Neutral sites get 0.

Performance: each ridge solve uses sparse matrices. The NCAA has about 360 teams and a few thousand games, so a solve takes milliseconds. Rebuilding ratings for a whole historical season, one snapshot per slate date, must take less than 2 minutes per season. Cache snapshots per slate date in `curated/ratings_snapshots/`.

## 3.3 Feature vector (per game; *h* = home, *a* = away)

| Group | Features |
|-------|----------|
| Strength | adj O, adj D, adj tempo for h and a; Elo h, a; Elo diff |
| Matchup | expected possessions `= f(T_h, T_a)`; expected PPP for each side `= μ + O_h − D_a (+HCA)`, and vice versa; the same for 1st half |
| Style | four factors (O and D) for both; 3PA rate; FT rate; matchup interactions (h ORB% × a DRB% allowed, etc.) |
| Confidence | games played h, a; prior weight h, a; season-to-date rating volatility |
| Situation | neutral flag, HCA estimate, rest days h/a (capped at 7), back-to-back flags, games in last 7 days, travel km since last game (if venue coords are known; else null), days since season start, postseason/conference-tournament flag |
| Form | last-5 opponent-adjusted margin residuals (actual − expected) |
| Availability (NBA) | share of team season minutes missing per the latest injury snapshot before `as_of` (status Out = 1.0, Doubtful = 0.75, Questionable = 0.4, configurable); the same for starters only |
| Calendar | month, is_weekend (ET) |

Every feature is named and versioned in `features/registry.py` with a `feature_set_version`. Each `FeatureRow` stores that version and a SHA-256 hash of its values (invariant 6).

## 3.4 Training dataset builder

`build_training_set(league, seasons)` replays every game in order and writes `curated/features/{league}/{season}.parquet`. Each row holds:
- the features
- the targets: home pts, away pts, 1H home, 1H away, OT flag
- metadata

It runs incrementally: only new games are appended each night.

## 3.5 The leakage test (invariant 1, fails CI)

1. **Mutation test.** For a sample of 500 games G:
   - compute `features_for_game(G)`
   - corrupt every game with `tip_time ≥ G.tip_time` (scramble scores, delete rows)
   - recompute
   - assert the result is byte-identical
2. **Hypothesis property test.** Generate random synthetic seasons and random `as_of` values. Assert that no rating or feature changes when future games are added or changed.
3. **Static check.** A grep-style CI rule bans direct access to the curated tables outside `features/` and `ratings/` data accessors, which all require `as_of`.

## Definition of Done

- [ ] Ratings snapshots built for all backfilled seasons.
- [ ] Training sets built for both leagues.
- [ ] All three leakage tests are in CI and passing.
- [ ] Ratings sanity report: the end-of-season top-25 per league looks plausible; ratings correlate with final win% > 0.85 (NBA) and > 0.8 (NCAA).
