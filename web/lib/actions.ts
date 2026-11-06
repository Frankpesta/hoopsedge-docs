"use server";

import { revalidatePath } from "next/cache";

import { ApiError, apiSend } from "./api";
import type { ActionResult, League, SettingsDocument, SettingsView } from "./types";

const LEAGUES = new Set<string>(["NBA", "NCAA"]);

async function attempt<T>(work: () => Promise<T>, paths: string[]): Promise<ActionResult<T>> {
  try {
    const data = await work();
    for (const path of paths) revalidatePath(path);
    return { ok: true, data };
  } catch (error) {
    if (error instanceof ApiError) return { ok: false, message: error.message };
    return { ok: false, message: "Couldn't reach the API. Nothing was changed." };
  }
}

function checkLeague(league: string): League {
  if (!LEAGUES.has(league)) throw new ApiError(422, "Unknown league");
  return league as League;
}

export async function runNow(
  league: string,
  slateDate: string,
  key: string,
): Promise<ActionResult<{ run_id: string }>> {
  return attempt(
    () =>
      apiSend<{ run_id: string }>(
        "POST",
        "/runs",
        { league: checkLeague(league), slate_date: slateDate },
        key,
      ),
    ["/", `/slate/${slateDate}`, "/runs"],
  );
}

export async function resolveUnmatched(
  eventId: string,
  choice: { team_id_home: string; team_id_away: string } | { ignore: true },
  key: string,
): Promise<ActionResult> {
  return attempt(
    () => apiSend("POST", `/unmatched/${encodeURIComponent(eventId)}/resolve`, choice, key),
    ["/unmatched"],
  );
}

export async function saveSettings(
  version: number,
  document: SettingsDocument,
  key: string,
): Promise<ActionResult<SettingsView>> {
  return attempt(
    () => apiSend<SettingsView>("PUT", "/config", { version, document }, key),
    ["/settings", "/system", "/"],
  );
}

export async function rollbackModel(
  league: string,
  version: string,
  reason: string,
  key: string,
): Promise<ActionResult> {
  return attempt(
    () => apiSend("POST", `/models/${checkLeague(league)}/rollback`, { version, reason }, key),
    ["/models"],
  );
}
