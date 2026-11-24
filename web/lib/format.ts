/** Display rules: times show in Lagos, slates are named by their US Eastern date. */

export const KIND_NAMES: Record<string, string> = {
  TOTALS_LEAN: "Totals lean",
  TEAM_TOTALS: "Team totals",
  TOTALS_ALL_OVER: "All overs",
  TOTALS_ALL_UNDER: "All unders",
};

export const FLAG_LABELS: Record<string, string> = {
  cross_level_game: "Cross-level game",
  cross_level_l5: "Cross-level last 5",
  provisional_tiers: "Provisional tiers",
  min_games: "Too few games",
};

export const MODE_LABELS: Record<string, string> = {
  main: "Main line",
  safest: "Safest line",
  safest_with_cushion: "Safest, with cushion",
};

/** Why a leg didn't make a ticket, in words (v2/legs.py, v2/tickets.py). */
export const DROP_REASONS: Record<string, string> = {
  started: "Game already started",
  inside_cutoff: "Too close to kickoff",
  lean_gone: "The line moved and the lean no longer holds",
  team_edge_gone: "The line moved and the team edge no longer holds",
  no_open_line: "No open line left",
  no_line_for_mode: "No line fits the line mode",
  beyond_top_games: "Past the top games by edge",
  ticket_below_min_legs: "Too few legs left for a ticket",
};

export function reasonLabel(code: string): string {
  return DROP_REASONS[code] ?? code;
}

const STATUS: Record<string, string> = {
  booked: "Booked",
  not_booked: "Not booked",
  verified: "Verified",
  dry_run: "Practice",
  invalid: "Invalid",
  failed: "Failed",
  superseded: "Superseded",
  pending_booking: "Booking",
  succeeded: "Succeeded",
  running: "Running",
  pending: "Pending",
  won: "Won",
  lost: "Lost",
  push: "Push",
  void: "Void",
};

export function statusLabel(status: string): string {
  return STATUS[status] ?? status;
}

export function formatOdds(odds: number): string {
  if (odds >= 1000) return Math.round(odds).toLocaleString("en-US");
  return odds.toFixed(2);
}

/** "~1 in 3,800": two significant figures, because more would claim false precision. */
export function oneInN(probability: number | null | undefined): string | null {
  if (probability === null || probability === undefined || probability <= 0) return null;
  if (probability >= 1) return "~1 in 1";
  const n = 1 / probability;
  const magnitude = 10 ** Math.max(0, Math.floor(Math.log10(n)) - 1);
  return `~1 in ${(Math.round(n / magnitude) * magnitude).toLocaleString("en-US")}`;
}

export function percent(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined) return "n/a";
  return `${(value * 100).toFixed(digits)}%`;
}

export function signedPercent(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined) return "n/a";
  const shown = (value * 100).toFixed(digits);
  return `${value >= 0 ? "+" : "−"}${shown.replace("-", "")}%`;
}

export function signedNumber(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined) return "n/a";
  return `${value >= 0 ? "+" : "−"}${Math.abs(value).toFixed(digits)}`;
}

const lagosTimeFormat = new Intl.DateTimeFormat("en-GB", {
  timeZone: "Africa/Lagos",
  weekday: "short",
  hour: "2-digit",
  minute: "2-digit",
});

const lagosDateTimeFormat = new Intl.DateTimeFormat("en-GB", {
  timeZone: "Africa/Lagos",
  day: "numeric",
  month: "short",
  hour: "2-digit",
  minute: "2-digit",
});

export function lagosTime(iso: string | null | undefined): string {
  if (!iso) return "n/a";
  const when = new Date(iso);
  return Number.isNaN(when.getTime()) ? "n/a" : lagosTimeFormat.format(when).replace(",", "");
}

export function lagosDateTime(iso: string | null | undefined): string {
  if (!iso) return "n/a";
  const when = new Date(iso);
  return Number.isNaN(when.getTime()) ? "n/a" : lagosDateTimeFormat.format(when).replace(",", "");
}

export function easternDate(now: Date = new Date()): string {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "America/New_York",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(now);
}

export function shiftDate(date: string, days: number): string {
  const when = new Date(`${date}T12:00:00Z`);
  when.setUTCDate(when.getUTCDate() + days);
  return when.toISOString().slice(0, 10);
}

export function longDate(date: string): string {
  return new Intl.DateTimeFormat("en-GB", {
    timeZone: "UTC",
    weekday: "long",
    day: "numeric",
    month: "long",
  }).format(new Date(`${date}T12:00:00Z`));
}

export function isValidDate(value: string): boolean {
  return (
    /^\d{4}-\d{2}-\d{2}$/.test(value) && !Number.isNaN(new Date(`${value}T12:00:00Z`).getTime())
  );
}

/** Exact paths and values: docs/10 wants every setting change previewed before it's saved. */
export function diffSettings(
  before: unknown,
  after: unknown,
  path = "",
): { path: string; from: unknown; to: unknown }[] {
  if (before && after && typeof before === "object" && typeof after === "object") {
    const left = before as Record<string, unknown>;
    const right = after as Record<string, unknown>;
    return [...new Set([...Object.keys(left), ...Object.keys(right)])]
      .sort()
      .flatMap((key) => diffSettings(left[key], right[key], path ? `${path}.${key}` : key));
  }
  return before === after ? [] : [{ path, from: before, to: after }];
}

/** Seconds between two timestamps, for run steps. */
export function secondsBetween(start: string, end: string | null | undefined): number | null {
  if (!end) return null;
  const seconds = (new Date(end).getTime() - new Date(start).getTime()) / 1000;
  return Number.isFinite(seconds) ? seconds : null;
}

/** "in 3 h 05 m", "in 12 m", or "started": for the earliest kickoff on a ticket. */
export function countdown(iso: string | null | undefined, now: Date = new Date()): string {
  if (!iso) return "all started";
  const minutes = Math.floor((new Date(iso).getTime() - now.getTime()) / 60000);
  if (!Number.isFinite(minutes)) return "n/a";
  if (minutes <= 0) return "started";
  const hours = Math.floor(minutes / 60);
  if (hours >= 48) return `in ${Math.floor(hours / 24)} days`;
  return hours ? `in ${hours} h ${String(minutes % 60).padStart(2, "0")} m` : `in ${minutes} m`;
}

/** One decimal, with a real minus sign: edges and projections are in points. */
export function points(value: number | null | undefined): string {
  if (value === null || value === undefined) return "";
  return `${value >= 0 ? "+" : "−"}${Math.abs(value).toFixed(1)}`;
}

export function oneDecimal(value: number | null | undefined): string {
  return value === null || value === undefined ? "" : value.toFixed(1);
}

/** "Delaware Over 23.5", or "Total Under 49.5" for a game total. */
export function legPick(leg: {
  team?: string | null;
  home: string;
  away: string;
  direction: string;
  booked_line: number;
}): string {
  const who = leg.team === "home" ? leg.home : leg.team === "away" ? leg.away : "Total";
  return `${who} ${leg.direction} ${leg.booked_line}`;
}

export const MARKET_NAMES: Record<string, string> = {
  totals: "Game totals",
  team_total: "Team totals",
};

const THRESHOLD_NAMES: Record<string, string> = {
  over_threshold: "Over",
  under_threshold: "Under",
  team_total_threshold: "Team",
};

/** "Over 4 · Under 5.5": thresholds in the owner's words. */
export function thresholdText(thresholds: Record<string, number>): string {
  return Object.entries(thresholds)
    .map(([name, value]) => `${THRESHOLD_NAMES[name] ?? name} ${value}`)
    .join(" · ");
}
