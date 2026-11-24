// A fake v2 API for browser tests: the real one's shapes, with state, and no real data at risk.
import { createServer } from "node:http";

const TODAY = new Intl.DateTimeFormat("en-CA", {
  timeZone: "America/New_York",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
}).format(new Date());
const LATER = new Date(Date.now() + 5 * 3600 * 1000).toISOString();
const EARLIER = new Date(Date.now() - 3600 * 1000).toISOString();
const NOTE =
  "Bookmaker-implied: the product of 1/odds for every leg, margin included. Each leg is close to a coin flip at main-line odds, so long tickets very rarely land (brief §13).";

function leg(event, home, away, team, booked, odds, started = false) {
  return {
    kind: team ? "TEAM_TOTALS" : "TOTALS_LEAN", event_id: event, home, away,
    kickoff: started ? EARLIER : LATER, started, mode: team ? "main" : "safest", team,
    direction: "Over", projection: team ? 28 : 61.9, booked_line: booked, main_line: booked + 1,
    odds, main_odds: 1.87, edge_main: 5.4, edge_booked: 6.4, flags: [],
  }; // prettier-ignore
}

function ticket(id, kind, code, status, legs, extra = {}) {
  return {
    ticket_id: id, sport: "NCAAB", slate: TODAY, run_id: "run-1", kind, number: 1, status, code,
    reason: status === "invalid" ? "2 legs in the code, 3 intended" : null,
    dry_run: status === "dry_run", total_odds: 3.03, bookmaker_implied_probability: 0.33,
    implied_note: NOTE, earliest_kickoff: legs[0].kickoff,
    next_kickoff: legs.find((item) => !item.started)?.kickoff ?? null,
    started_legs: legs.filter((item) => item.started).length, model_disagrees: 0,
    superseded_by: null, rebooked_by: null, rebook_of: null, current: true, legs, ...extra,
  }; // prettier-ignore
}

const LEAN = ticket("T-lean", "TOTALS_LEAN", "H4NDC4", "booked", [
  leg("e1", "Duke", "UNC", null, 140.5, 1.74),
  leg("e2", "Kansas", "Baylor", null, 150.5, 1.74, true),
]);
const TEAM = ticket("T-team", "TEAM_TOTALS", "DRYRUN-ABC123", "dry_run", [
  leg("e1", "Duke", "UNC", "home", 75.5, 1.85),
  leg("e2", "Kansas", "Baylor", "away", 70.5, 1.85),
]);
const OLD = ticket("T-old", "TOTALS_LEAN", "OLD123", "superseded", [leg("e1", "Duke", "UNC", null, 139.5, 1.7)], {
  current: false, superseded_by: "run-1", result: "lost",
}); // prettier-ignore

function row(event, home, away, lean, flags = [], started = false) {
  return {
    event_id: event, game_id: `g-${event}`, home, away, kickoff: started ? EARLIER : LATER, started,
    eligible: true, skip_reason: null, home_tiers: ["High", "Medium"], away_tiers: ["Medium", "Low"],
    home_l5: { pf: 80.2, pa: 70.1, n: 5 }, away_l5: { pf: 75.4, pa: 72.8, n: 5 }, sum_of_avgs: 155.6,
    model_total: 149.3, outlook: "MEDIUM", main_line: 143.5, lean, edge_main: 5.8,
    booked_line: lean === "OVER" ? 140.5 : null, booked_odds: lean === "OVER" ? 1.74 : null,
    edge_booked: lean === "OVER" ? 8.8 : null, home_proj: 76.5, away_proj: 72.8,
    team_pick: { team: home, side: "home", direction: "Over", line: 75.5, odds: 1.85, projection: 79.6, edge: 4.1 },
    flags, same_level_total: 149.3, same_level_lean: lean,
  }; // prettier-ignore
}

const run = (sport, status = "succeeded") => ({
  run_id: `run-${sport}`, sport, slate: TODAY, trigger: "scheduled", status, dry_run: false,
  error: null, started_at: new Date(Date.now() - 60_000).toISOString(),
  ended_at: status === "running" ? null : new Date().toISOString(), config_version: 0,
  counts: { events_listed: 76, games: 2, eligible: 2, unmatched: 1, tickets: 2 }, tickets: 2,
}); // prettier-ignore

const state = {
  ncaabRun: run("NCAAB"),
  polls: 0,
  started: false,
  unmatched: [
    {
      sport: "NCAAB", event_id: "sr:match:9", home_name: "Duke Blue Devils",
      away_name: "Mystery Tech Owls", start: LATER, reason: "team_not_resolved",
      home_team_id: "150", away_team_id: null, home_suggestions: [],
      away_suggestions: [{ team_id: "2", name: "Auburn Tigers", score: 60 }],
    },
  ], // prettier-ignore
  aliases: [
    { alias: "penn quakers", team_id: "219", team_name: "Pennsylvania Quakers", source: "seed" },
  ],
  config: null,
  version: 0,
  versions: [],
};

const SCHEDULES = {
  backup: "05:00", ncaaf_scores: "08:00", ncaab_scores: "08:00", nba_scores: "08:00",
  ncaaf_slate: "18:00", ncaaf_saturday_slate: "14:00", ncaab_slate: "16:00",
  ncaab_late_slate: "21:00", nba_slate: "16:00", nba_late_slate: "21:00",
}; // prettier-ignore

function sportConfig(over, provisional) {
  return {
    enabled: true, sport_id: "sr:sport:2", tournament_ids: ["sr:tournament:648"],
    espn: { path: "basketball/mens-college-basketball", groups: [50] }, l5_window: 5, min_games: 4,
    l5_same_level_only: false, over_threshold: over, under_threshold: 5, team_total_threshold: 4,
    tiers: provisional ? "provisional_percentiles" : { atk: [24, 33], def: [20, 28], provisional: false },
    line_mode: { totals: "safest", team_totals: "main" }, min_cushion: 0,
  }; // prettier-ignore
}

state.config = {
  sports: { NCAAF: sportConfig(4, false), NCAAB: sportConfig(5, true), NBA: sportConfig(5, true) },
  outlook: { low_max: 2, medium_max: 5 },
  tickets: {
    max_legs: 20, min_legs: 2, max_games_per_kind: 100, booking_cutoff_minutes: 20,
    kinds: { TOTALS_LEAN: true, TEAM_TOTALS: true, TOTALS_ALL_OVER: false, TOTALS_ALL_UNDER: false },
  }, // prettier-ignore
  timezone_display: "Africa/Lagos",
  dry_run: true,
  schedules: SCHEDULES,
  pause_all: false,
  sportybet_proxy_enabled: false,
};

function numbers(legs, hit, implied, roi, perSlate) {
  const won = Math.round(legs * hit);
  return {
    legs, won, lost: legs - won, push: 0, hit_rate: hit, implied, above_implied: hit - implied,
    roi, per_slate: perSlate,
  }; // prettier-ignore
}

function calibration(sport) {
  const config = state.config.sports[sport];
  const current = {
    over_threshold: config.over_threshold,
    under_threshold: config.under_threshold,
  };
  return {
    sport, graded_legs: 640, pending_legs: 12, slates: 9, min_graded: 100,
    tickets: { won: 1, lost: 6, pending: 2 },
    suggestions: [
      { market: "totals", mode: "main", same_level: false, graded: 320, slates: 9,
        current: { thresholds: current, numbers: numbers(90, 0.53, 0.53, -0.02, 10) },
        suggested: { thresholds: { over_threshold: 4, under_threshold: 6.5 },
                     numbers: numbers(140, 0.56, 0.53, 0.04, 15.6) } },
      { market: "totals", mode: "safest", same_level: false, graded: 320, slates: 9,
        current: { thresholds: current, numbers: numbers(90, 0.7, 0.71, -0.03, 10) }, suggested: null },
      { market: "team_total", mode: "main", same_level: false, graded: 300, slates: 9,
        current: { thresholds: { team_total_threshold: 4 }, numbers: numbers(120, 0.55, 0.53, 0.02, 13) },
        suggested: { thresholds: { team_total_threshold: 4 }, numbers: numbers(120, 0.55, 0.53, 0.02, 13) } },
    ],
    tables: [
      { market: "totals", mode: "main", same_level: false, bucket: "0–2", cross_level: false,
        ...numbers(80, 0.5, 0.53, -0.06, 9) },
      { market: "totals", mode: "main", same_level: false, bucket: "4–6", cross_level: true,
        ...numbers(40, 0.6, 0.53, 0.11, 4) },
    ],
  }; // prettier-ignore
}

function slate() {
  const ncaab = {
    sport: "NCAAB", run: state.ncaabRun,
    rows: [row("e1", "Duke", "UNC", "OVER", ["cross_level_l5"]), row("e2", "Kansas", "Baylor", "PASS", [], true)],
    tickets: [LEAN, TEAM, OLD], unmatched: state.unmatched.length, csv: true,
  }; // prettier-ignore
  const empty = (sport) => ({ sport, run: null, rows: [], tickets: [], unmatched: 0, csv: false });
  return { slate_date: TODAY, sports: [empty("NBA"), ncaab, empty("NCAAF")] };
}

function send(response, status, body) {
  response.writeHead(status, { "content-type": "application/json" });
  response.end(JSON.stringify(body));
}

const server = createServer(async (request, response) => {
  const url = new URL(request.url, "http://x");
  const path = url.pathname;
  if (request.headers["x-hoopsedge-key"] !== "e2e-key") return send(response, 401, {});
  let raw = "";
  for await (const chunk of request) raw += chunk;
  const body = raw ? JSON.parse(raw) : null;

  if (request.method === "GET") {
    if (path.endsWith("/csv")) {
      response.writeHead(200, {
        "content-type": "text/csv",
        "content-disposition": `attachment; filename="ncaab_predictions_${TODAY.replaceAll("-", "")}.csv"`,
      });
      return response.end("Away,Home,Model Total\nUNC,Duke,149.3\n");
    }
    if (path.startsWith("/slates/")) {
      if (state.started) {
        state.polls += 1;
        // Running for a few polls, then done, as a real run would be.
        state.ncaabRun = { ...run("NCAAB", state.polls < 3 ? "running" : "succeeded") };
        if (state.polls >= 3) state.started = false;
      }
      return send(response, 200, slate());
    }
    if (path.startsWith("/tickets/")) {
      const found = [LEAN, TEAM, OLD].find((item) => item.ticket_id === path.split("/")[2]);
      return found
        ? send(response, 200, found)
        : send(response, 404, { detail: "Ticket not found" });
    }
    if (path === "/runs") return send(response, 200, [state.ncaabRun]);
    if (path.startsWith("/runs/")) {
      return send(response, 200, {
        ...state.ncaabRun, tickets_detail: [LEAN, TEAM], unmatched_events: [], failures: [],
        dropped: { inside_cutoff: 1, ticket_below_min_legs: 2 },
      }); // prettier-ignore
    }
    if (path === "/unmatched") return send(response, 200, state.unmatched);
    if (path.startsWith("/teams/")) {
      return send(response, 200, [
        { team_id: "150", name: "Duke Blue Devils", short_name: "Duke", level: "D1" },
        { team_id: "2", name: "Auburn Tigers", short_name: "Auburn", level: "D1" },
        { team_id: "219", name: "Pennsylvania Quakers", short_name: "Penn", level: "D1" },
      ]);
    }
    if (path.startsWith("/aliases/")) return send(response, 200, state.aliases);
    if (path === "/config")
      return send(response, 200, { version: state.version, document: state.config, diff: [] });
    if (path === "/config/versions") return send(response, 200, state.versions);
    if (path.startsWith("/calibration/"))
      return send(response, 200, calibration(path.split("/")[2]));
    if (path === "/system") {
      return send(response, 200, {
        cost: null, budget_usd: [10, 25, 50],
        alerts: [{ sk: `${new Date().toISOString()}#run-failed`, severity: "CRITICAL", message: "NBA run failed", ref: "r" }],
        freshness: { last_scheduled_job: null, ncaab_scores: new Date().toISOString(), ncaab_run: state.ncaabRun.started_at },
        dry_run: state.config.dry_run, scheduled_dry_run: true, pause_all: state.config.pause_all,
        relay: { mode: "proxy", reachable: true, checked_at: new Date().toISOString() },
        golive: {
          days_needed: 3, practice_clean: true, live_done: false, ready: false,
          days: ["2026-11-25", "2026-11-26", "2026-11-27"].map((day) => ({
            day, clean: true, problems: [],
            sports: { NBA: { runs: 2, failed: 0, tickets: 1 }, NCAAB: { runs: 2, failed: 0, tickets: 9 }, NCAAF: { runs: 1, failed: 0, tickets: 2 } },
          })),
          live_codes: { NBA: { code: "NBA123", run_id: "r", slate: "2026-11-27", booked_at: new Date().toISOString(), rebook: false }, NCAAB: null, NCAAF: null },
        },
      }); // prettier-ignore
    }
    return send(response, 404, { detail: "Not found" });
  }
  if (request.method === "POST" && path === "/runs") {
    state.started = true;
    state.polls = 0;
    return send(response, 202, {
      sport: body.sport,
      slate_date: body.slate_date,
      started_at: new Date().toISOString(),
    });
  }
  if (request.method === "POST" && path.endsWith("/rebook")) {
    return send(response, 202, {
      ticket_id: path.split("/")[2],
      sport: "NCAAB",
      slate_date: TODAY,
    });
  }
  if (request.method === "POST" && path.endsWith("/resolve")) {
    state.unmatched = [];
    return send(response, 200, { event_id: "sr:match:9", status: "resolved", aliases: {} });
  }
  if (request.method === "POST" && path === "/aliases") {
    const added = {
      alias: body.name.toLowerCase(),
      team_id: body.team_id,
      team_name: "Auburn Tigers",
      source: "owner",
    };
    state.aliases = [...state.aliases, added];
    return send(response, 200, added);
  }
  if (request.method === "DELETE" && path.startsWith("/aliases/")) {
    state.aliases = state.aliases.filter((item) => item.source !== "owner");
    response.writeHead(204);
    return response.end();
  }
  if (request.method === "POST" && path.endsWith("/apply")) {
    if (body.version !== state.version)
      return send(response, 409, { detail: "The config changed since you loaded it" });
    const sport = path.split("/")[2];
    state.version += 1;
    state.config = {
      ...state.config,
      sports: {
        ...state.config.sports,
        [sport]: {
          ...state.config.sports[sport],
          ...body.thresholds,
          line_mode: { ...state.config.sports[sport].line_mode, totals: body.mode },
        },
      },
    };
    return send(response, 200, { version: state.version, document: state.config, diff: [] });
  }
  if (request.method === "PUT" && path === "/config") {
    if (body.version !== state.version)
      return send(response, 409, { detail: "The config changed since you loaded it" });
    if (body.document.tickets.booking_cutoff_minutes < 20) {
      return send(response, 422, { detail: "tickets.booking_cutoff_minutes: must be at least 20" });
    }
    state.version += 1;
    state.config = body.document;
    state.versions = [
      {
        version: state.version,
        changed_at: new Date().toISOString(),
        changed_by: "dashboard",
        diff: [],
      },
      ...state.versions,
    ];
    return send(response, 200, { version: state.version, document: state.config, diff: [] });
  }
  return send(response, 404, { detail: "Not found" });
});

server.listen(8100, "127.0.0.1");
