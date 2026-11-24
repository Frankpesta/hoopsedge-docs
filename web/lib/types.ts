import type { components } from "./api-types";

type Schemas = components["schemas"];

export type Sport = Schemas["Sport"];
export type Slate = Schemas["Slate"];
export type SlateSport = Schemas["SlateSport"];
export type SlateRow = Schemas["SlateRow"];
export type TicketView = Schemas["TicketView"];
export type LegView = Schemas["LegView"];
export type RunSummary = Schemas["RunSummary"];
export type RunDetail = Schemas["RunDetail"];
export type UnmatchedItem = Schemas["UnmatchedItem"];
export type Team = Schemas["Team"];
export type AliasView = Schemas["AliasView"];
export type ConfigView = Schemas["ConfigView"];
export type ConfigVersion = Schemas["ConfigVersion"];
export type SystemView = Schemas["SystemView"];
export type GoLive = Schemas["GoLive"];
export type Calibration = Schemas["Calibration"];
export type Suggestion = Schemas["Suggestion"];
export type CalibrationRow = Schemas["CalibrationRow"];

export const SPORTS: readonly Sport[] = ["NCAAF", "NCAAB", "NBA"];

export function isSport(value: string | null | undefined): value is Sport {
  return SPORTS.includes(value as Sport);
}

/** The config document's shape. The API validates every save, so this only types the form. */
export type SportConfig = {
  enabled: boolean;
  sport_id: string;
  tournament_ids: string[];
  espn: { path: string; groups: number[] };
  l5_window: number;
  min_games: number;
  l5_same_level_only: boolean;
  over_threshold: number;
  under_threshold: number;
  team_total_threshold: number;
  tiers: { atk: [number, number]; def: [number, number]; provisional?: boolean } | string;
  line_mode: { totals: string; team_totals: string };
  min_cushion: number;
};

export type ConfigDocument = {
  sports: Record<Sport, SportConfig>;
  outlook: { low_max: number; medium_max: number };
  tickets: {
    max_legs: number;
    min_legs: number;
    max_games_per_kind: number;
    booking_cutoff_minutes: number;
    kinds: Record<string, boolean>;
  };
  timezone_display: string;
  dry_run: boolean;
  schedules: Record<string, string>;
  pause_all: boolean;
  sportybet_proxy_enabled: boolean;
};

export type ActionResult<T = unknown> = { ok: true; data: T } | { ok: false; message: string };
