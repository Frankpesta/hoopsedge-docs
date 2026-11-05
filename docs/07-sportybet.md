# Phase 6 — SportyBet: Offers, Matching, Booking, Verification

**Goal:** a robust client for SportyBet Nigeria that reads offers and creates and verifies **booking codes**. It must never log in or place a bet (D11).

All IDs, URLs and field names come from `sportybet/markets.yaml` and `sportybet/endpoints.yaml`, which are written in Phase 0. If SportyBet changes its API, only those files and the parsers change.

## 6.1 Client (`sportybet/client.py`)

- httpx with a 15 s timeout, the shared rate limiter (1 req/s for SportyBet), retries on 429/5xx/timeouts with jitter, and browser-like headers recorded in Phase 0.
- Optional proxy: when `sportybet_proxy_enabled`, load the proxy URL from SSM. The proxy applies **only** to this client.
- If Phase 0 found a required cookie/token, add a session bootstrap: a GET of the landing page, cached for 20 min.
- Every raw response goes to `s3://…/raw/sportybet/{run_id}/{call}-{ts}.json`.
- **Guard:** a CI test greps the `sportybet/` package for anything matching `login|password|wallet|deposit|placeBet|/orders/(?!share)` and fails the build if found.

## 6.2 Offers (`sportybet/offers.py`)

1. Fetch upcoming basketball events for the NBA and NCAA tournament IDs. Cover the slate window: tip times within the ET slate date ± 6 h.
2. For each event, fetch (or read from the list response) the four market families. Collect all specifiers if `allow_alt_lines`, else only what is needed to determine the main line.
3. Normalize into the `Offer` model from doc 06, including `status` and `fetched_at`.
4. Schema validation with pydantic. An unknown market shape is a **loud failure** for that event: it is recorded and skipped, never guessed.

## 6.3 Matching (`teams/matcher.py`, invariant 5)

For each SportyBet event:
1. Normalize the home and away names, then look up exact aliases.
2. On a miss, run rapidfuzz `token_set_ratio` against the league's aliases. A side is a candidate only if the score is ≥ 92 and the margin over the runner-up is ≥ 5.
3. Find canonical games on the slate whose teams match and whose tip time is within ±3 h of SportyBet's start time.
4. **Auto-match only if** both sides resolve and exactly one game matches. Also verify orientation: SportyBet home = canonical home. Neutral-site games may list either team first. If they are swapped, flip the handicap sign and team sides, and record `orientation=swapped`.
5. Everything else becomes an `UNMATCHED#` item with its top-3 suggestions. Confirming one in the dashboard writes a confirmed alias (`confirmed_by=owner`), so the same name auto-matches from then on.

Test with a fixture table of at least 60 tricky NCAA name pairs, including deliberately wrong pairs that must **not** match.

## 6.4 Booking (`sportybet/booking.py`)

```python
def book(ticket: Ticket) -> BookingResult  # returns code or raises BookingError
```

- Build the selections payload exactly as Phase 0 recorded it.
- If `DRY_RUN`: return `code="DRYRUN-{ticket_id[:6]}"` without calling the network, and mark the ticket `dry_run`.
- Idempotency: before booking, check the `TICKET#` item. If it already has a verified code for the identical selection hash, reuse that code instead of booking again.
- If SportyBet rejects a selection (suspended, odds changed): drop that leg, re-check `min_legs`, and retry once with the reduced ticket. Record the dropped leg with reason `REJECTED_AT_BOOKING`.

## 6.5 Verification (invariant 4)

1. Immediately after booking, decode the code via the share lookup endpoint.
2. Compare the sets `{event_id, market_id, specifier, outcome_id}`. Also compare the odds within a tolerance of 0.02 and record any difference.
3. On an exact set match, set `verified=true`. Any mismatch, missing selection or lookup failure sets the ticket to `invalid`, sends an alert, and the code is shown struck-through on the dashboard with the reason.

## 6.6 Fresh-odds re-check (invariant 3)

The `RecheckOffers` step re-fetches every event on the built tickets no more than 5 min before `Book`:
- **Specifier changed:** re-resolve the main line → re-score → keep the leg only if it is still eligible.
- **Odds moved:** re-score edge/EV. Drop the leg if it is below threshold.
- **Event no longer `not_started`, or tip too close:** drop.
- Then rebuild the tickets for that market (the rank order may change). Record each change in the run log.

## Definition of Done

- [ ] All of the above works against the recorded fixtures. respx-mocked tests cover every failure path.
- [ ] One live end-to-end run in `dev` with `DRY_RUN=false` **for a single 2-leg ticket**, approved by the owner beforehand, producing a code that verifies.
- [ ] The no-betting guard test is in CI.
