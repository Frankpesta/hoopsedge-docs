# Phase 7 — Grading, Continuous Learning & Model Registry

**Goal:** the system learns after every game and improves without ever silently getting worse (D9).

## 7.1 Grading (`grade` Lambda, every 30 min after ingest)

For each `LEG#` with status `pending` whose game is final (per ESPN):
- Compute the settlement value from the final score, or the 1st-half score for `total_1h`. Overtime counts for "incl. overtime" markets per `markets.yaml`.
- Set the status to `won|lost|push|void`. `void` applies when a game is postponed or cancelled and still not played 36 h after scheduled tip. The rule is configurable; follow SportyBet's published rule if Phase 0 recorded it.
- Store `actual_value` and `model_error = actual − predicted_mean`.

Ticket status:
- `lost` as soon as any leg is lost
- `won` when all legs are settled with none lost (push/void legs count at odds 1.0)
- otherwise `pending`

Also store `settled_combined_odds`. Grading is idempotent and never re-grades a settled leg unless a score correction is detected. A correction is logged and flagged on the dashboard.

## 7.2 Learning after every game (no model refit)

When new finals arrive, `ratings` recomputes the current-season snapshots (doc 04). The next prediction for any team immediately reflects every game played, including their opponents' games. This is the "learns from every game" loop and it costs seconds.

## 7.3 Nightly learning job (`learn`, 11:00 Lagos, after all US games are final)

Per league:

1. **Compact** the day's delta Parquet into the season files. Append new rows to the training set.
2. **Recalibrate:**
   - Fit new per-market calibrators on a rolling window of the last 60 days of **live, out-of-sample** predictions (graded legs plus all scored offers, not only selected ones), topped up with walk-forward predictions from last season when n < 1,000.
   - Candidate calibrator vs current: compare log loss on the most recent 14 days. Note the overlap honestly; for a clean comparison, fit on days 15–60 and evaluate on days 1–14.
3. **Retrain the challenger:** refit all model families with the committed hyperparameters on all seasons + current season to date.
4. **Champion vs challenger evaluation** on a holdout neither model trained on:
   - Holdout = the most recent 21 days of current-season games. The challenger is trained **excluding** them for this test only. After promotion, the final artifact is refit including them.
   - Metrics: log-likelihood of final scores (primary), CRPS, and Brier per market family.
   - Promotion rule:
     - the challenger's mean log-likelihood improvement > 0 **and** its paired bootstrap 90% CI lower bound > 0 (or above a small `−ε` tolerance on CRPS, while **no** market family's Brier gets worse by more than 0.002)
     - **and** holdout n ≥ 150 games (NBA) / 400 (NCAA)
   - If n is too small (early season), do not promote. The champion keeps running, with fresh ratings feeding it.
5. **Promote or reject:** write a `MODEL#` audit item with all metrics and the decision. Update the `CHAMPION#{league}` pointer on promotion.
6. **Drift monitor:** compare the last 7 days of mean residual (bias) and σ ratio against the backtest. If |bias| > 2 pts or the σ ratio falls outside [0.85, 1.15], raise a warning alert and flag it on the dashboard.
7. **Error analysis report** (JSON for the dashboard): residuals by team, line size, favourite/underdog, home/away/neutral, rest, and early vs late season, plus hit rate per market × probability bucket.

If training would exceed 12 min, follow the documented Fargate Spot fallback (same image, `learn` entrypoint). Do not enable it unless it is needed.

## 7.4 Model registry (S3 + DynamoDB)

```
s3://…/models/{league}/{version}/
   ft_mean.txt  ft_var.txt  h1_mean.txt  h1_var.txt   # LightGBM boosters
   calibrators/{family}.json
   params.yaml  feature_set_version.txt  training_data_hash.txt
   metrics.json  evidence.json
```

- `version` = `{yyyymmdd-hhmm}-{gitsha[:7]}`.
- DynamoDB `CHAMPION#{league}` → `{version, calibrator_version, promoted_at, reason}`.
- **Rollback:** `POST /models/{league}/rollback {version}` (doc 09) repoints the champion and writes an audit item.
- Lambdas load the champion at cold start and re-check the pointer every 10 min.

## 7.5 Tests

- Grading on every market and edge case: integer-line push, 1H settlement, OT, postponement, score correction.
- Promotion logic with synthetic metric inputs: promote, reject on small n, reject on a single-market regression.
- Registry round-trip and rollback.

## Definition of Done

- [ ] Grading runs in `dev` across 3 days of real games, and the results match a hand-check of 20 legs.
- [ ] Nightly learning executes end-to-end in < 12 min per league, writes audit items and evidence JSON.
- [ ] Rollback demonstrated.
