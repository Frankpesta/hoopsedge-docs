import type { components } from "./api-types";

type Schemas = components["schemas"];

export type Slate = Schemas["Slate"];
export type SlateLeague = Schemas["SlateLeague"];
export type TicketCard = Schemas["TicketCard"];
export type LegSummary = Schemas["LegSummary"];
export type TicketDetail = Schemas["TicketDetail"];
export type LegDetail = Schemas["LegDetail"];
export type GameDetail = Schemas["GameDetail"];
export type RunSummary = Schemas["RunSummary"];
export type RunDetail = Schemas["RunDetail"];
export type TrackRecord = Schemas["TrackRecord"];
export type ModelsView = Schemas["ModelsView"];
export type Evidence = Schemas["Evidence"];
export type UnmatchedItem = Schemas["UnmatchedItem"];
export type Team = Schemas["Team"];
export type SettingsView = Schemas["SettingsView"];
export type SettingsDocument = Schemas["SettingsDocument"];
export type SystemView = Schemas["SystemView"];
export type League = Schemas["League"];
export type Family = Schemas["Family"];

export type ActionResult<T = unknown> = { ok: true; data: T } | { ok: false; message: string };
