# Phase 4 — Models, Calibration & Walk-Forward Evidence

**Goal:** models that output a **full joint distribution** of both teams' scores (full game and 1st half), and an evidence report proving they beat simple baselines on games they never saw.

## 4.1 Model families (one set per league)

| Family | Targets | Notes |
|--------|---------|-------|
| `ft_mean` | `E[home_pts]`, `E[away_pts]` | LightGBM regressors (two models, or one model on a stacked team-perspective dataset with a home flag; pick by backtest) |
| `ft_var` | margin σ, total σ, ρ(home_pts, away_pts) | heteroscedastic: LightGBM on squared out-of-fold residuals (log target), floored/capped per league |
| `h1_mean` | `E[home_1h]`, `E[away_1h]` | same approach on 1st-half targets |
| `h1_var` | 1H margin σ, 1H total σ, ρ | same |
| `baseline` | ratings-only expected scores | `μ + O_h − D_a + HCA` × expected possessions; no ML. Always computed; used as fallback reference and as a feature sanity check |

**Distribution:** bivariate normal on (home_pts, away_pts) parameterized from mean, σ_margin, σ_total. From it derive the margin, total and team-total distributions analytically.

**Integer lines and pushes:** use a continuity correction, `P(X = k) ≈ Φ((k+0.5−μ)/σ) − Φ((k−0.5−μ)/σ)`.

**Overtime:** SportyBet "incl. overtime" markets settle on the final score. Training targets are final scores (OT included), so OT is absorbed. Also evaluate an explicit OT mixture (P(tie at regulation) × OT points distribution) and keep it only if the backtest improves log-likelihood on totals.

**Alternative tried in the backtest:** an empirical residual distribution (kernel-smoothed out-of-fold residuals, scaled by predicted σ) instead of the normal. Pick whichever has better CRPS. Record the choice in DECISIONS.

## 4.2 Hyperparameters & training

- LightGBM, objective `regression` (or `huber` if it backtests better).
- Monotone constraints where they make sense: higher own adjusted O → higher own expected points; higher opponent adjusted D (worse defence) → higher own expected points.
- Tuning: Optuna, **small budget** (≤ 40 trials per family), on a single walk-forward fold. Train on seasons ≤ S−2, validate on S−1. This runs once locally, and the chosen parameters are committed in `models/params/{league}.yaml`. Nightly learning only refits with those parameters; it does not re-tune.
- Sample weights: up-weight recent seasons (half-life of 2 seasons). Down-weight the COVID-flagged seasons (0.5).
- Training time budget: must finish in < 10 min on Lambda at 10 GB. NCAA has the most rows, around 60k games × 2; this is fine for LightGBM.

## 4.3 Probability calibration

Market probabilities come out of the distribution (doc 06). They are then passed through a **per league × market-family calibrator**:
- isotonic regression when there are ≥ 1,000 calibration samples
- Platt scaling otherwise

Calibrators are fit on out-of-fold / walk-forward predictions only, never on in-sample fits. Calibrators are versioned separately from the mean/variance models, so they can be refreshed nightly (doc 08).

## 4.4 Walk-forward backtest (`backtest/`)

Simulate the live system exactly:

```
for season in test_seasons (last 3 seasons per league):
  model trained on all seasons < season
  for each slate date d in season:
     ratings/features as of each game's tip (Phase 3 code)
     predict distributions
     at end of day: (optional) daily refit simulation every 7 days to mimic continuous learning
```

**Metrics (per league, per season, per market family):**
- **Score models:** MAE and RMSE of margin and total; CRPS; Gaussian log-likelihood.
- **Market probabilities against synthetic lines.** Without historical market lines, evaluate at lines placed at the model's own median ± {0, 2.5, 5}, plus the actual final distribution. Check the Brier score, log loss and **reliability diagram** (10 bins).
- **Against real historical lines (where Phase 0 found them):**
  - cover/over hit-rate of picks passing `min_prob`/`min_edge`
  - CLV-style comparison of the model line against the market line
  - simulated flat-stake ROI at −110-equivalent prices
- **Baselines to beat** (required):
  1. the ratings-only `baseline`
  2. Elo-only margin with a league-average total
  3. "last-season prior only" (no current-season updates), which proves current-season learning adds value

**Threshold tuning:** sweep `min_prob` ∈ [0.52, 0.65] and `min_edge` ∈ [0, 0.05] on validation seasons only. Choose the values maximizing calibrated log-likelihood of the selected legs subject to ≥ N legs per slate. Write the chosen values to config defaults and DECISIONS.

**Early-season check (D1):** report the metrics separately for each team's first 1–5, 6–10 and 11+ games, to tune the prior weight `k` and the regression `r`.

## 4.5 Evidence report (`docs/reports/evidence-{league}-{date}.md` + JSON for the dashboard)

The report contains:
- Data coverage.
- A metric table against the baselines, with **95% bootstrap confidence intervals**.
- Reliability diagrams (PNG) per market.
- Hit rate by probability bucket.
- Early-season breakdown.
- Feature importance (gain + SHAP summary on a sample).
- A plain-English verdict: for each market and league, either "beats baselines with CI excluding zero" or "does not beat baselines".

**Gate:** a market × league combination that does **not** beat baseline 1 on log loss is set `enabled=false` in config by default. The dashboard shows it as "insufficient evidence". The owner can override this, and the override is logged.

## Definition of Done

- [ ] All model families trained for both leagues. Artifacts are saved in the registry format (doc 08).
- [ ] Evidence reports generated and committed. Gate decisions recorded.
- [ ] Unit tests: distribution math (known μ/σ → known probabilities, push handling), calibrator monotonicity, training determinism (fixed seed → identical artifact hash).
