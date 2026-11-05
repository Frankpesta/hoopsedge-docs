# Phase 9 — Dashboard (Next.js 15 on Vercel)

**Goal:** a beautiful, creative, fast dashboard, used mostly **on a phone**, that makes the booking codes effortless to copy and the evidence impossible to miss. Read the `frontend-design` guidance if available. Avoid a templated "admin template" look.

## 9.1 Architecture

- App Router, Server Components fetch from the API with `HOOPSEDGE_API_KEY`, using `server-only` and `lib/api.ts`. **No API key or AWS URL in client bundles.** A test greps `.next/static` after the build.
- Mutations go through Server Actions.
- Data freshness: `revalidate: 30` on slate pages. A "Refresh" button calls `router.refresh()`. While a run is in progress, poll every 5 s (client component with SWR) until it finishes.
- Optional Vercel Deployment Protection (password) can be added later. It is not required (D8).

## 9.2 Visual direction: "Courtside Terminal"

- **Mood:** a late-night broadcast stats desk. Deep ink background (#0B0D12 range), with a hardwood-amber accent for primary actions and win states. Cool slate neutrals. A desaturated red for losses/invalid states. A light theme also exists, but dark is the default.
- **Type:**
  - a characterful condensed display face for headings and team names (e.g. "Big Shoulders Display" or "Barlow Condensed")
  - a clean grotesk for body text ("Inter Tight" or "Geist")
  - a **tabular monospace** for numbers and booking codes ("JetBrains Mono" / "Geist Mono")
  - load via `next/font`
- **Signature elements:**
  - Booking codes are shown as large, letter-spaced "ticket stubs" with a perforated edge (CSS mask). Tap anywhere copies; haptic-style press animation; "Copied" toast.
  - A subtle court-line SVG motif in section headers.
  - Probability bars styled as shot-clock segments.
  - Framer Motion: staggered card entrance, number tick-ups, layout animations when tickets update. Respect `prefers-reduced-motion`.
- **Accessibility:** WCAG AA contrast; focus rings; every chart has a text summary.

## 9.3 Pages

1. **Today `/`** (and `/slate/[date]`)
   - Header: slate date (ET) with the Lagos time of the first tip, run status chips per league, and a "Run now" button per league with a confirmation sheet.
   - Tabs for league (NBA | NCAA), then a segmented control for the market: Handicap · FT O/U · HT O/U · Team O/U.
   - Ticket cards showing:
     - the code stub
     - number of legs, combined odds and **combined probability** ("~1 in 3,800"), EV
     - status: `verified` / `dry_run` / `invalid` / `superseded`
     - version
     - an expandable legs list: matchup, pick (e.g. "Duke −6.5"), odds, model probability vs implied probability, edge, tip time (Lagos)
   - An "Excluded" drawer: counts by reason code, plus a link to the unmatched queue.
   - An empty state for when no market has enough legs. The empty state explains why, so it doesn't look like an error.
2. **Ticket `/tickets/[id]`:** a full leg table; live/settled status per leg once grading starts (✓ ✕ push); version history diff.
3. **Game `/games/[id]`:**
   - Predicted score, with the margin and total distribution chart (Recharts area with the line marked, showing win/push/lose regions).
   - 1st-half projection.
   - Ratings comparison bars.
   - Top feature drivers (from SHAP, if stored).
   - NBA injuries.
   - All SportyBet outcomes scored.
4. **Track record `/track-record`:**
   - Filters: league, market, date range.
   - KPI tiles: leg hit rate vs required breakeven, push %, calibration error, tickets won, nearest misses.
   - Reliability diagram.
   - Hit rate by probability bucket.
   - Rolling 14-day hit rate.
5. **Model lab `/models`:**
   - Champion per league.
   - Learning curve (log-likelihood / Brier over time with version markers).
   - Challenger history table with promote/reject reasons.
   - Rollback button.
   - Evidence-report summary with the "beats baseline?" verdict per market.
   - Drift warnings.
6. **Unmatched `/unmatched`:** a card per event with the SportyBet names and a one-tap pick from the suggestions, or a searchable team picker.
7. **Runs `/runs`:** a timeline of pipeline executions; per-step durations; errors; dropped-leg logs.
8. **Settings `/settings`:**
   - schedules
   - thresholds per market
   - league/market toggles
   - `min_legs`
   - `allow_alt_lines`
   - proxy flag
   - dry-run toggle (with a loud banner while it's on)

   Every change shows a diff preview and is audited.
9. **System `/system`:** AWS cost month-to-date vs budget, data freshness, recent alerts.

## 9.4 Quality

- Vitest for formatters (odds, probability → "1 in N", Lagos times) and components.
- Playwright smoke tests against a mocked API (MSW): copy a code; switch markets; resolve an unmatched item; run-now flow.
- Lighthouse mobile performance ≥ 90 on Today.

## Definition of Done

- [ ] All pages built against the real `dev` API.
- [ ] Mobile-first layouts checked at 375 px.
- [ ] Secret-leak test passes.
- [ ] The owner signs off on the visual design.
