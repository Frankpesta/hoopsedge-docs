"use server";

import { revalidatePath } from "next/cache";

import { ApiError, apiDelete, apiSend } from "./api";
import { isSport } from "./types";
import type { ActionResult, AliasView, ConfigDocument, ConfigView, Sport } from "./types";

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

function checkSport(sport: string): Sport {
  if (!isSport(sport)) throw new ApiError(422, "Unknown sport");
  return sport;
}

export async function runNow(
  sport: string,
  slateDate: string,
  key: string,
): Promise<ActionResult<{ started_at: string }>> {
  return attempt(
    () =>
      apiSend<{ started_at: string }>(
        "POST",
        "/runs",
        { sport: checkSport(sport), slate_date: slateDate },
        key,
      ),
    ["/", `/slate/${slateDate}`, "/runs"],
  );
}

export async function rebookTicket(ticketId: string, key: string): Promise<ActionResult> {
  return attempt(
    () => apiSend("POST", `/tickets/${encodeURIComponent(ticketId)}/rebook`, {}, key),
    ["/", `/tickets/${ticketId}`],
  );
}

export async function resolveUnmatched(
  eventId: string,
  choice: { team_id_home?: string; team_id_away?: string } | { ignore: true },
  key: string,
): Promise<ActionResult> {
  return attempt(
    () => apiSend("POST", `/unmatched/${encodeURIComponent(eventId)}/resolve`, choice, key),
    ["/unmatched"],
  );
}

export async function addAlias(
  sport: string,
  name: string,
  teamId: string,
  key: string,
): Promise<ActionResult<AliasView>> {
  return attempt(
    () =>
      apiSend<AliasView>(
        "POST",
        "/aliases",
        { sport: checkSport(sport), name, team_id: teamId },
        key,
      ),
    ["/unmatched"],
  );
}

export async function deleteAlias(
  sport: string,
  alias: string,
  key: string,
): Promise<ActionResult> {
  return attempt(
    () => apiDelete(`/aliases/${checkSport(sport)}/${encodeURIComponent(alias)}`, key),
    ["/unmatched"],
  );
}

export async function saveConfig(
  version: number,
  document: ConfigDocument,
  key: string,
): Promise<ActionResult<ConfigView>> {
  return attempt(
    () => apiSend<ConfigView>("PUT", "/config", { version, document }, key),
    ["/settings", "/system", "/"],
  );
}
