// A fake API for browser tests: the real one's shapes, with state, and no real data at risk.
import { createServer } from "node:http";

const TODAY = new Intl.DateTimeFormat("en-CA", {
  timeZone: "America/New_York",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
}).format(new Date());
const TIP = "2026-11-07T01:00:00+00:00";
const NOTE =
  "Unproven: this market's probabilities haven't been tested at real bookmaker lines, and when they were tested on full-game totals, confident picks won about half the time. Treat each leg as about 50%.";

function leg(rank, pick, game, hidden, odds = 1.9) {
  return {
    rank,
    game_id: game,
    matchup: "UNC Tar Heels at Duke Blue Devils",
    pick,
    odds,
    p_calibrated: hidden ? null : 0.57,
    fair: 0.5,
    edge: hidden ? null : 0.07,
    tip_time: TIP,
  };
}

function ticket(id, family, number, code, status, hidden, picks) {
  return {
    ticket_id: id,
    league: "NCAA",
    family,
    ticket_no: number,
    version: 2,
    status,
    settle_status: "pending",
    booking_code: code,
    reason: status === "invalid" ? "1 selections missing, 0 unexpected" : null,
    legs: picks.length,
    combined_odds: 3.61,
    combined_prob: hidden ? null : 0.32,
    expected_value: hidden ? null : 0.16,
    passed_gate: !hidden,
    edge_label: hidden ? "unproven" : "measured",
    probability_note: hidden ? NOTE : null,
    correction: false,
    legs_summary: picks.map((pick, index) => leg(index + 1, pick, `g${index + 1}`, hidden)),
  };
}

const run = (league, status = "succeeded") => ({
  run_id: `run-${league}`,
  league,
  slate_date: TODAY,
  status,
  trigger: "scheduled",
  dry_run: false,
  started_at: "2026-11-06T14:00:00+00:00",
  ended_at: "2026-11-06T14:00:21+00:00",
  error: null,
  counts: {},
});

const state = {
  ncaaRun: run("NCAA"),
  polls: 0,
  unmatched: [
    {
      league: "NCAA",
      event_id: "sr:match:99",
      home_name: "Mystery Tech",
      away_name: "UNC Tar Heels",
      start: TIP,
      reason: "team_not_resolved",
      home_team_id: null,
      away_team_id: "a",
      home_suggestions: [{ team_id: "h", score: 80 }],
      away_suggestions: [],
    },
  ],
  settingsVersion: 1,
  settings: {
    schedules: { ncaa_slate: "15:00", nba_slate: "22:00", nightly_learn: "11:00" },
    leagues: { NBA: { enabled: true }, NCAA: { enabled: true } },
    markets: Object.fromEntries(
      ["NBA", "NCAA"].map((league) => [
        league,
        {
          handicap: { enabled: true, min_prob: 0.52, min_edge: 0.025 },
          total_ft: {
            enabled: true,
            min_prob: league === "NBA" ? null : 0.52,
            min_edge: league === "NBA" ? null : 0.015,
          },
          total_1h: { enabled: true, min_prob: 0.52, min_edge: 0 },
          team_total: { enabled: true, min_prob: 0.64, min_edge: 0 },
        },
      ]),
    ),
    min_legs: 2,
    min_minutes_before_tip: 20,
    allow_alt_lines: false,
    sportybet_proxy_enabled: false,
    dry_run: false,
    pause_all: false,
  },
  rollbacks: [],
};

const TICKETS = [
  ticket("handicap-1", "handicap", 1, "H4NDC4", "verified", false, ["Duke -6.5", "Kansas +3"]),
  ticket("total-1", "total_ft", 1, "T0TAL1", "invalid", false, ["Over 145.5", "Under 150.5"]),
  ticket("half-1", "total_1h", 1, "DRYRUN-ABC123", "dry_run", true, [
    "1H Under 75.5",
    "1H Over 66.5",
  ]),
];

function slate(date) {
  const empty = { handicap: [], total_ft: [], total_1h: [], team_total: [] };
  const today = date === TODAY;
  return {
    date,
    leagues: [
      {
        league: "NBA",
        run: null,
        games: today ? 9 : 0,
        first_tip: today ? TIP : null,
        markets: empty,
        excluded: {},
        eligible_legs: 0,
        unmatched_events: 0,
      },
      {
        league: "NCAA",
        run: today ? state.ncaaRun : null,
        games: today ? 60 : 0,
        first_tip: today ? TIP : null,
        markets: today
          ? { ...empty, handicap: [TICKETS[0]], total_ft: [TICKETS[1]], total_1h: [TICKETS[2]] }
          : empty,
        excluded: today ? { LOW_PROB: 449, NO_BOOKABLE_LINE: 8 } : {},
        eligible_legs: today ? 6 : 0,
        unmatched_events: today ? state.unmatched.length : 0,
      },
    ],
  };
}

const models = (league) => ({
  league,
  champion: {
    version: "20261106-1953-4d25ce0",
    promoted_at: "2026-11-06T20:13:07+00:00",
    reason: "First champion: the newest clean evidence model",
  },
  history: [],
  learning_curve: [
    {
      at: "2026-11-06T20:13:07+00:00",
      version: "20261106-1953-4d25ce0",
      decision: "bootstrap",
      log_likelihood_gain: null,
      crps_gain: null,
      games: null,
      reasons: ["First champion"],
    },
    {
      at: "2026-11-05T10:00:00+00:00",
      version: "20261105-1000-older00",
      decision: "rejected",
      log_likelihood_gain: -0.0005,
      crps_gain: -0.005,
      games: 185,
      reasons: ["Mean log-likelihood gain is -0.00050, not above zero"],
    },
  ],
  drift: { flags: ["ft_margin bias is +2.40 points"] },
  gates: {
    handicap: {
      passed: false,
      edge: "measured",
      reason: "does not beat baselines",
      override: { decision: "D65" },
    },
    total_ft: {
      passed: false,
      edge: "measured",
      reason: "does not beat baselines",
      override: { decision: "D65" },
    },
    total_1h: { passed: true, edge: "unproven", reason: "beats baselines", override: null },
    team_total: { passed: true, edge: "unproven", reason: "beats baselines", override: null },
  },
});

const track = {
  league: null, market: null, first: null, last: null,
  legs: 40, won: 20, lost: 18, push: 2, void: 0,
  hit_rate: 0.526, push_rate: 0.05, breakeven_hit_rate: 0.524,
  flat_stake_profit: 0.3, flat_stake_roi: 0.008, calibration_error: 0.04,
  tickets: { won: 1, lost: 6, pending: 1 }, missed_by: { 1: 3, 2: 3 },
  legs_won_on_lost: 20, legs_on_lost: 34,
  buckets: [
    { bucket: 0.5, legs: 18, won: 9, hit_rate: 0.5, mean_probability: 0.53 },
    { bucket: 0.55, legs: 12, won: 7, hit_rate: 0.58, mean_probability: 0.57 },
    { bucket: 0.6, legs: 8, won: 4, hit_rate: 0.5, mean_probability: 0.62 },
  ],
  daily: [
    { date: "2026-11-05", legs: 12, won: 6, lost: 6 },
    { date: "2026-11-06", legs: 14, won: 8, lost: 5 },
    { date: "2026-11-07", legs: 14, won: 6, lost: 7 },
  ],
}; // prettier-ignore

const game = {
  game_id: "g1",
  league: "NCAA",
  game: { tip_utc: TIP, home_id: "h", away_id: "a", slate_date_et: TODAY },
  home_name: "Duke Blue Devils",
  away_name: "UNC Tar Heels",
  final: null,
  prediction: {
    ft: { mu_home: 78, mu_away: 71, sd_margin: 11.5, sd_total: 15, sd_home: 9, sd_away: 9 },
    h1: { mu_home: 38, mu_away: 34, sd_margin: 8, sd_total: 10, sd_home: 6, sd_away: 6 },
  },
  key_features: { adj_o_h: 112.4, adj_o_a: 108.1, adj_d_h: 97.2, adj_d_a: 101.5, adj_t_h: 34.1, adj_t_a: 33.2, elo_h: 1650, elo_a: 1580, form_h: 2.1, form_a: -0.4, rest_h: 2, rest_a: 3, games_h: 3, games_a: 3 },
  injuries: [],
  lines: [
    { family: "handicap", line: -6.5, specifier: "hcp=-6.5" },
    { family: "total_ft", line: 148.5, specifier: "total=148.5" },
  ],
  rejections: [{ family: "total_1h", side: "over", specifier: "total=70.5", reason: "LOW_PROB", probability_note: NOTE }],
  legs: [{ family: "handicap", side: "home", specifier: "hcp=-6.5", p_calibrated: 0.57, edge: 0.07 }],
  edge_labels: { handicap: "measured", total_ft: "measured", total_1h: "unproven", team_total: "unproven" },
}; // prettier-ignore

function ticketDetail(id) {
  const card = TICKETS.find((item) => item.ticket_id === id);
  if (!card) return null;
  return {
    ticket: card,
    run_id: "run-NCAA",
    slate_date: TODAY,
    selection_hash: "abc",
    settled_combined_odds: null,
    verification: { missing: [], extra: [], odds_differences: [] },
    leg_details: card.legs_summary.map((item) => ({
      ...item, event_id: "e", family: card.family, specifier: "x=1", line: 1, side: "over",
      outcome_id: "12", status: item.rank === 1 ? "won" : "pending", actual_value: item.rank === 1 ? 150 : null,
      model_error: item.rank === 1 ? 3 : null, correction: false,
    })), // prettier-ignore
    versions: [
      {
        ticket_id: id,
        version: 2,
        status: card.status,
        booking_code: card.booking_code,
        run_id: "run-NCAA",
      },
    ],
  };
}

function send(response, status, body) {
  response.writeHead(status, { "content-type": "application/json" });
  response.end(JSON.stringify(body));
}

const server = createServer(async (request, response) => {
  const url = new URL(request.url, "http://x");
  const path = url.pathname;
  if (request.headers["x-hoopsedge-key"] !== "e2e-key")
    return send(response, 401, { detail: "Unauthorized" });
  let raw = "";
  for await (const chunk of request) raw += chunk;
  const body = raw ? JSON.parse(raw) : null;

  if (request.method === "GET") {
    if (path.startsWith("/slates/")) {
      // A run in progress finishes after a couple of polls, as a real one would.
      // Like a real one, it first takes a moment to record itself, then runs, then ends.
      if (state.starting) {
        state.polls += 1;
        if (state.polls === 2) state.ncaaRun = { ...run("NCAA", "running"), run_id: "new-run" };
        if (state.polls >= 4) {
          state.ncaaRun = { ...run("NCAA"), run_id: "new-run" };
          state.starting = false;
        }
      }
      return send(response, 200, slate(path.split("/")[2]));
    }
    if (path.startsWith("/tickets/")) {
      const detail = ticketDetail(path.split("/")[2]);
      return detail
        ? send(response, 200, detail)
        : send(response, 404, { detail: "Ticket not found" });
    }
    if (path.startsWith("/games/"))
      return path === "/games/g1"
        ? send(response, 200, game)
        : send(response, 404, { detail: "Game not found" });
    if (path === "/runs")
      return send(response, 200, [
        run("NCAA"),
        { ...run("NBA", "failed"), run_id: "run-bad", error: "BlockedError: 403" },
      ]);
    if (path.startsWith("/runs/")) {
      return send(response, 200, {
        run: run("NCAA"),
        steps: [
          {
            name: "FetchOffers",
            started: "2026-11-06T14:00:00+00:00",
            ended: "2026-11-06T14:00:04+00:00",
          },
          {
            name: "Predict",
            started: "2026-11-06T14:00:04+00:00",
            ended: "2026-11-06T14:00:21+00:00",
          },
        ],
        log: [{ step: "RecheckOffers", event: "LEG_DROPPED", payload: { game_id: "g1" } }],
      });
    }
    if (path === "/track-record")
      return send(
        response,
        200,
        url.searchParams.get("league") === "NBA" ? { ...track, legs: 0, won: 0, lost: 0 } : track,
      );
    if (path.startsWith("/models/")) return send(response, 200, models(path.split("/")[2]));
    if (path === "/unmatched") return send(response, 200, state.unmatched);
    if (path.startsWith("/teams/"))
      return send(response, 200, [
        { team_id: "h", name: "Duke Blue Devils" },
        { team_id: "a", name: "UNC Tar Heels" },
      ]);
    if (path === "/config")
      return send(response, 200, {
        version: state.settingsVersion,
        document: state.settings,
        changed_at: TIP,
        changed_by: "dashboard",
        diff: [],
      });
    if (path === "/system") {
      return send(response, 200, {
        cost: null,
        budget_usd: [10, 25, 50],
        alerts: [
          { sk: `${TIP}#run-failed`, severity: "CRITICAL", message: "BlockedError: 403", ref: "r" },
        ],
        freshness: { last_ingest: TIP, last_odds_fetch: TIP, last_scheduled_job: null },
        dry_run: state.settings.dry_run,
        scheduled_dry_run: true,
        pause_all: state.settings.pause_all,
      });
    }
  }
  if (!request.headers["idempotency-key"])
    return send(response, 400, { detail: "Mutations need an idempotency-key header" });
  if (request.method === "POST" && path === "/runs") {
    state.starting = true;
    state.polls = 0;
    return send(response, 202, {
      run_id: "new-run",
      league: body.league,
      slate_date: body.slate_date,
    });
  }
  if (request.method === "POST" && path.endsWith("/resolve")) {
    state.unmatched = [];
    return send(response, 200, { event_id: "sr:match:99", status: "resolved", aliases: {} });
  }
  if (request.method === "PUT" && path === "/config") {
    if (body.version !== state.settingsVersion)
      return send(response, 409, { detail: "Settings changed since you loaded them" });
    state.settings = body.document;
    state.settingsVersion += 1;
    return send(response, 200, {
      version: state.settingsVersion,
      document: state.settings,
      diff: [],
    });
  }
  if (request.method === "POST" && path.endsWith("/rollback")) {
    return send(response, 200, { league: "NCAA", version: body.version, reason: body.reason });
  }
  return send(response, 404, { detail: "Not found" });
});

server.listen(8100, "127.0.0.1");
