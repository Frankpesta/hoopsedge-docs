# HoopsEdge: End-to-End Build Plan

Written 2026-11-05 from `CLAUDE.md` and docs 00–12. This plan says how the build is sequenced and what the docs leave unresolved. The numbered docs remain the spec, and this file does not repeat them.

**Status:** owner reviewed on 2026-11-05. C1–C5, C14 and Q1–Q3 are decided and logged in `docs/DECISIONS.md` as D12–D19. The other items in §4 are still proposals, to be logged when the phase that needs them starts.

## 1. How we will work

- **Local first (D20):** everything is built and run in Docker on the owner's machine. AWS deployment comes last, so the effective order is Phase 0 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9 → 1 → 10. Phase 2 and later use local stand-ins for S3 and DynamoDB. Soak checks that say `dev` run locally first, then again after deployment.
- **Branches and commits (D21):** each feature gets its own branch from `main`. The owner makes all commits. Claude Code stops at the end of each feature with a summary, the test results and a suggested commit message, and starts the next feature only after the owner has committed.
- **Phase gates:** phases run in order. A phase starts only when the previous Definition of Done (DoD) is met. Anything the owner must do is listed as a gate.
- **Tests first:** tests for each invariant are written before the code they protect.
- **Git-dependent steps:** these only work once the owner has committed and pushed:
  - `deploy.yml` and the "deploy from CI" DoD in Phase 1
  - the model version string, which includes the git SHA (`{yyyymmdd-hhmm}-{gitsha[:7]}`). Builds from uncommitted code get a `-dirty` suffix and cannot be promoted.

## 2. Current environment (checked 2026-11-05)

| Item | Finding | Action |
|------|---------|--------|
| Git | Repo initialized on `main`, no commits, identity set | Owner makes the first commit |
| `core.autocrlf` | `true` | `.gitattributes` forces LF so container scripts work |
| Docker | CLI 29.9.2 installed, **daemon not reachable** | Owner starts Docker Desktop |
| Python | Local version is 3.15; the project pins 3.12 | Run Python in Docker, not on the host |
| `uv`, `cdk`, `gh` | Not installed | `uv` and `cdk` run in containers. `gh` is optional; the GitHub web UI also works |
| Node | Local version is 26, not an LTS release | Pin an LTS version for `infra/` and `web/` that CDK and Vercel both support |
| CPU | AMD64; Lambda targets arm64 | See §3 |
| AWS CLI | Signed in as `frank-fps-deploy`, default region `eu-west-1` | **Not used for HoopsEdge (D12).** Owner sets up a new account and `hoopsedge` profile. CDK pins its region explicitly so the CLI default cannot apply |
| Lambda limits | Could not be read: this user lacks `lambda:GetAccountSettings` | Check in the chosen account (see Phase 1) |

## 3. Docker strategy

Docker provides the same pinned toolchain on the owner's machine and in CI. It stays local apart from the Lambda image the docs already specify, so it adds no AWS services and no paid services.

| Piece | Purpose |
|-------|---------|
| `services/Dockerfile`, target `lambda` | The single arm64 Lambda image from doc 02, under 1 GB, with no training-only packages |
| `services/Dockerfile`, target `dev` | Python 3.12, `uv` and dev extras (Optuna, plotting) on the native architecture. Used for pytest, ruff, mypy, `scripts/`, backfill and tuning |
| `docker-compose.yml`, `dev` | Interactive toolchain with the repo and `./data` mounted |
| `docker-compose.yml`, `aws-mock` | `moto_server` for S3, DynamoDB, SSM and SNS, so a full pipeline can run locally with no AWS access or cost |
| `docker-compose.yml`, `collector` | The early data collector from §5, Phase 0 (approved, D13) |
| `docker-compose.yml`, `node` | LTS Node for CDK synth, Vitest, Playwright and `next dev` |

Notes:

- **Native vs arm64:** day-to-day work uses native AMD64 for speed, because arm64 under QEMU is slow on this machine. CI builds and smoke-tests the arm64 image, including `import lightgbm` and invoking a handler through the Lambda Runtime Interface Emulator built into the base image.
- **Local pipeline runner:** Step Functions Local is not worth depending on. `scripts/run_pipeline_local.py` calls the real `pipeline.handler` for each step in the order the state machine does. A CDK test asserts both use the same step list, so they cannot drift apart.

## 4. Doc conflicts and gaps to resolve

Severity: **High** can produce misleading evidence or break an invariant. **Med** will fail in deployment or operation. **Low** is a clean-up.

### Model validity

| # | Sev | Problem | Proposed resolution |
|---|-----|---------|---------------------|
| C1 | High | **Results that were not yet known count as known.** Doc 04 uses `as_of = tip` and filters on `tip_time < as_of`. In replay, a game that tipped an hour earlier contributes its final score, though it had not finished. Live runs happen around 09:00–10:00 ET (NCAA) and 13:30–14:30 ET (NBA), before nearly all of that day's games. Training would see fresher ratings than production, and backtests would look better than reality. | Keep invariant 1 and add a stricter rule: a result is usable only if final **and** `result_known_at < as_of`, with `result_known_at = tip + 3 h` in replay. Training and backtests set `as_of` to the slate's scheduled run time. Add a test that an unfinished earlier game cannot change features. |
| C2 | High | **Injury features are empty for all of history.** ESPN's injuries endpoint shows only the current state, and snapshots start when we start collecting. The model cannot learn from an always-null feature, yet injuries matter a lot for NBA lines. Using a game's actual absences would break invariant 1. | V1: keep the feature as null in training and rely on the `NBA_INJURY_UNCERTAIN` gate. Start collecting snapshots now. Activate the feature when enough live data exists, through the normal challenger process. Phase 4 may test a separate adjustment fitted on seasons before the test season, and keep it only if it beats the baseline. |
| C3 | High | **Champion vs challenger is not a fair test.** Doc 08 calls the 21-day holdout unseen by both models, but a recently promoted champion was refit on those days, and that advantage would block promotions. Hyperparameters are also fixed, so the only real difference is training cutoff. | Evaluate only when the champion's stored training cutoff is earlier than the holdout start; otherwise skip and record why. Promotion therefore happens about every 21 days at most. Holding out recent games always leaves the challenger's cutoff behind, so this also needs a test on synthetic data. |
| C4 | High | **Synthetic-line scores are not comparable.** Doc 05 places lines at "the model's own median". Each candidate would be scored on different lines, so the baseline gate would not be like for like. Every model also sits near 50% at its own median. | Score all candidates on one shared line set: real market lines where available, otherwise baseline median ± offsets. Define the baseline's σ as the league constant from training seasons. |
| C5 | High | **Calibration may not fit real selections.** At a main line the market implies about 50%, so `min_prob = 0.56` already means an edge near 6%. `min_edge = 0.015` then does nothing. Legs that pass are those where the model most disagrees with the market, and that is often model error. A calibrator fitted only on synthetic lines would not correct for this. | Treat ESPN `pickcenter` coverage (Phase 0.4) as a key finding. Calibrate on real lines where possible. If history has none, label market-edge claims "unproven" until live logs build up, and rely on the market gates in the meantime. |

### Infrastructure and operations

| # | Sev | Problem | Proposed resolution |
|---|-----|---------|---------------------|
| C6 | Med | **Nightly learning won't fit one run.** One 900 s Lambda with concurrency 1 cannot run both leagues at the "< 12 min per league" budget. | One execution per league, in sequence, through a small state machine or staggered schedules. |
| C7 | Med | **Account limits can block deployment.** Reserved concurrency totals 14, and AWS requires 10 to stay unreserved, so the account needs a limit of at least 24. Some newer accounts have a limit of 10 and also cap Lambda memory at 3,008 MB, which would block the 10 GB `learn` function. | Check at the start of Phase 1 and request a quota increase if needed (free, but can take time). |
| C8 | Med | **ECR ordering.** Doc 02 puts the ECR repo in `ComputeStack`, but those functions need an image at creation. CDK's Docker asset alternative uses a shared repo that skips the keep-3 rule. | Move ECR to its own `EcrStack`. Deploy order: `EcrStack`, push image, then `ComputeStack`. |
| C9 | Med | **Lock expiry.** DynamoDB TTL deletion can lag by days, so a crashed run could block later runs well past its 30 minutes. | Make the conditional put also succeed when `ttl < now`. `MarkRunFailed` releases the lock. |
| C10 | Med | **Freshness window is tight at 1 req/s.** About 150 NCAA events per-event means about 150 s, before booking and verifying up to 20 tickets (about 40 s more) and any retries. | Use a bulk endpoint if Phase 0 finds one. Check `STALE_ODDS` per ticket immediately before each booking call, not once per step. |
| C11 | Med | **`grade-and-ingest` has no orchestrator.** It chains ingest, grade and ratings, but the docs define only the slate state machine. | Add a small `maintenance` state machine. At about 6,600 transitions a month in total, the cost is about $0.07 over the free tier. |
| C12 | Med | **Dry-run settings conflict.** Doc 00 defaults config to `dry_run: false`, while invariant 8 requires true in local and dev. Doc 07 also needs one live booking in `dev`. | Booking happens only if both the environment and config allow it, so either one can force dry-run. The `dev` live test uses a one-time, owner-approved override that expires, limited to one 2-leg ticket. |
| C13 | Med | **Superseded tickets could double-count.** Legs stay in `PENDINGLEG`, so re-runs would inflate the track record. | Grade every leg, but count only legs on the latest non-superseded version in `AGG#`. |
| C14 | Med | **Open dashboard controls.** Under D8 the API secret is safe, but anyone who finds the Vercel URL can use Run now, change config, turn off dry-run or roll back a model. | Decided: stays open, per D8. Risk accepted by the owner (D19). |
| C15 | Low | **NBA promotion threshold is borderline.** 21 days yields about 150 NBA games, and the requirement is n ≥ 150, so the All-Star break alone would block promotion. | Use a 28-day NBA holdout, or keep the rule and accept fewer promotions. Decide in Phase 7. |
| C16 | Low | **Missing entities in doc 12:** team records, the ingest cursor, idempotency keys and a mutation rate counter. An in-memory limit won't work across 5 concurrent Lambdas. | Add `TEAM#`, `CURSOR#`, `IDEMP#` and `RATE#` items, with TTL where appropriate. |
| C17 | Low | **`/health` contradiction.** Doc 09 says it returns "no data" but lists champion versions and recent runs, without a secret. | Without the secret, return only `{ok}`. Move the details behind the secret. |
| C18 | Low | **Mutation rule omits PUT.** Doc 09 says mutations are POST with an idempotency key, but `/config` and `/schedules` use PUT. | Apply the idempotency and rate rules to every mutating method. |
| C19 | Low | **`pause_all` doesn't stop schedules.** Doc 10 implies it does, but schedules keep firing. | Handlers exit early when paused and log it. The doc's weekly off-season ingest is exempt. |
| C20 | Low | **Sort and determinism details.** `v10` sorts before `v2` in ticket keys. LightGBM artifact hashes also differ across CPU architectures. | Zero-pad versions (`v0007`). Test determinism within one architecture, with `deterministic=true` and fixed threads. |

## 5. Phase plan

### Phase 0: Feasibility (nothing else proceeds without this)

The most likely way this project fails is that SportyBet blocks automation or its data doesn't fit the spec, so this comes first.

1. Set up Docker `dev`, `uv` and a pinned lock file. This is the minimum needed to run `scripts/`.
2. **SportyBet discovery:** record endpoints, market and outcome IDs, specifier formats, main-line rule, selection limit, same-event rule and status values. Never log in. Create at most 5 test codes.
3. Check how many NCAA games offer each of the four market families. HT and Team O/U may be sparse, which would limit D3's volume.
4. **Geo test:** owner's machine first, then a throwaway Lambda in `us-east-1`, deleted afterwards. If there is a JS challenge, **stop and report**, as doc 01 requires. We will not try to defeat one.
5. **ESPN probe:** half-score and box-score coverage, depth to 2015-16, how `dates=` treats late games, sustainable request rate, D1 count on a busy day.
6. **Historical lines:** `pickcenter` coverage per league and season, which directly affects C4 and C5.
7. **Early collector (approved, D13):** injuries, SportyBet offers and ESPN lines cannot be recovered later, and both seasons are underway. A small Docker service on the owner's machine would snapshot them to `./data/` until AWS ingest takes over. It uses the same rate limits and the same client code.

**Outputs:** `markets.yaml`, `endpoints.yaml`, scrubbed fixtures, `docs/reports/phase0-data-coverage.md`, DECISIONS entries.
**Owner gate:** approve test bookings and the throwaway Lambda, then review the DECISIONS entries.
**Stop condition:** if booking or verification isn't feasible, pause and revisit the plan. Later phases depend on it.

### Phase 1: Infrastructure

- Tests first: CDK cost-assertion test (doc 02 §1.6), then the stacks with `EcrStack` split out (C8).
- Stub handlers, the Dockerfile, a hello-world pipeline that writes a `RUN` item, and `common/` (config, logging, IDs, ET/Lagos time, errors).
- `ci.yml` and `deploy.yml` with OIDC, plus `docs/reports/cost-estimate.md`. The largest compute cost is `learn`: at its full 15-minute limit for both leagues, it uses about 540,000 GB-s a month. That exceeds Lambda's 400,000 GB-s free tier by itself and would cost roughly $2 a month, which still fits the $10 target.

**Owner gates:** AWS account, alert email, GitHub repo, OIDC role, confirm SNS and budget emails, quota checks (C7).

### Phase 2: Data ingestion and teams

- Tests first: parsers on fixtures, validation rules, reject handling.
- `common/http.py`, ESPN clients, curated writers, team registry, aliases, `NON_D1` pool.
- **Backfill in Docker:** about 80,000 requests at 2 req/s, so **about 11 hours** of resumable running. Then sync curated Parquet to S3.
- Deploy live ingest. The collector hands over here.

**Soak:** 48 hours of live ingest in `dev` with no failures.

### Phase 3: Ratings and features

- Tests first: all three leakage tests, including the C1 rule, before any ratings code.
- Ridge solves with prior-based shrinkage, Elo, HCA, four factors, 1H ratings, feature registry and hashing, training-set builder.
- Training and live prediction share one code path, and a test asserts it.

**Check:** sanity report, with win% correlation above 0.85 (NBA) and 0.8 (NCAA).

### Phase 4: Models and evidence

- Tests first: distribution math, push handling, calibrator monotonicity, per-architecture determinism.
- Baselines first, then LightGBM mean and variance families, calibrators and Optuna tuning (run locally).
- Walk-forward backtest with C1, C4 and C5 applied. Evidence reports with bootstrap CIs and market gates.
- Compute SHAP values with LightGBM's built-in `pred_contrib`, to keep the image small.

**Owner gate:** read the evidence verdicts and confirm the market gates. Some market and league combinations may well fail and stay disabled, as designed.

### Phase 5: Market probabilities and ticket engine

- Pure functions, no network or AWS. Property tests, partition edge cases (0 to 250 legs) and a golden-file test on a recorded slate.

### Phase 6: SportyBet client

- Tests first: the no-betting grep guard, matcher tests with at least 60 tricky name pairs (including pairs that must **not** match), and verification mismatch paths.
- Offers, matching, booking (dry-run default), code verification, fresh-odds re-check, `Unmatched` queue, then the full pipeline wired up.

**Owner gate:** explicitly approve one live 2-leg booking in `dev`.

### Phase 7: Grading and learning

- Tests first: all settlement cases, and promotion logic on synthetic inputs (C3, C15).
- Grading (C13), compaction, recalibration, challenger training, registry, rollback, drift monitor, error analysis. Per-league execution (C6).

**Soak:** 3 days of real games, plus a hand check of 20 legs by the owner.

### Phase 8: API

- FastAPI with Mangum, secret middleware, all endpoints using queries only (no scans), `AGG#` reads, and OpenAPI types with a drift check in CI.
- Tests: moto, the 401 path, a check that LightGBM and polars aren't imported in the API path, and p95 under 300 ms warm.

### Phase 9: Dashboard

- "Courtside Terminal" design, mobile-first at 375 px. Today and ticket stubs come first, because copying codes is the core daily task.
- Then game, track record, model lab, unmatched, runs, settings and system pages.
- Secret-leak test on `.next/static`, Vitest, Playwright with MSW, Lighthouse.

**Owner gates:** connect the Vercel project, set environment variables, sign off the visual design.

### Phase 10: Operations and go-live

- Alert deduplication, daily digest, README runbook, `pause_all`.
- **7 consecutive days** of `dev` dry-run with both leagues and all markets, no CRITICAL alerts, all pseudo-tickets graded.
- One supervised live booking per league, with codes checked in the SportyBet app. Then deploy `prod`, enable its schedules and disable `dev` schedules.

## 6. Decisions needed from the owner

| # | Decision | Answer or recommendation | Needed by |
|---|----------|--------------------------|-----------|
| Q1 | Which AWS account and credentials? | **Decided (D12):** new separate account with a `hoopsedge` CLI profile | Phase 0 geo test |
| Q2 | Run the early collector (§5, Phase 0, step 7)? | **Decided (D13):** yes | Phase 0 |
| Q3 | Protect the dashboard URL (C14)? | **Decided (D19):** leave open, per D8 | Phase 9 |
| Q4 | Approve C1–C5 for the model design? | **Decided (D14–D18):** all approved | Before Phase 3 |
| Q5 | Which email receives alerts and budget notices? | Owner's choice | Phase 1 |
| Q6 | GitHub repo for CI and OIDC, and is it private? | Private. It will contain SportyBet integration details | Phase 1 |

## 7. Main risks

| Risk | Effect | Mitigation |
|------|--------|------------|
| SportyBet blocks AWS, changes its API or objects to automation | Booking stops | Phase 0 test, config-driven IDs, loud failures, proxy flag, local-agent option. These are unofficial APIs, so this risk stays open for the life of the project |
| ESPN's unofficial endpoints change | Ingest stops | Raw responses saved before parsing, stale-ingest alert, CBBD fallback for NCAA |
| No edge over market lines | Tickets lack positive EV | Market gates and live logging. This is a real possibility: a good score model can still fail to beat the market |
| Few legs qualify | Far fewer than 100 legs per market | Expected and accepted by D4. The dashboard explains empty states |
| Season timing | NCAA began this week, so priors dominate early predictions | D1 priors, `LOW_CONFIDENCE` gate, early-season breakdown in evidence |
| Cost creep | Credits run down | CDK assertions, budgets, reserved concurrency, daily cost snapshot |

### What the tickets can realistically deliver

Long accumulators rarely win, even when every leg is good. Assuming independent legs:

| Legs | Per-leg probability | Chance the ticket wins |
|------|---------------------|------------------------|
| 20 | 56% | about 1 in 110,000 |
| 20 | 60% | about 1 in 27,000 |
| 20 | 65% | about 1 in 5,500 |
| 5 | 60% | about 1 in 13 |

The bookmaker's margin also compounds with every leg. The dashboard shows these odds plainly, as doc 06 requires. Leg hit rate and calibration are better measures of the model than ticket wins.

## 8. Immediate next steps

Phase 0 feature branches, in order:

1. `feature/dev-environment`: pinned Docker toolchain
2. `feature/espn-probe`: ESPN coverage proof and historical-lines check
3. `feature/sportybet-discovery`: endpoints, markets and fixtures
4. `feature/data-collector`: the early collector (D13)

The owner is creating the new AWS account and `hoopsedge` profile. It is needed for the geo test and for deployment, not for the local build.
