import { NextResponse } from "next/server";

import { ApiError, apiGetFresh } from "@/lib/api";
import { isValidDate } from "@/lib/format";
import type { Slate } from "@/lib/types";

/** For the browser's polling while a run is in progress. The key stays here on the server. */
export async function GET(_: Request, context: { params: Promise<{ date: string }> }) {
  const { date } = await context.params;
  if (!isValidDate(date)) return NextResponse.json({ detail: "Bad date" }, { status: 422 });
  try {
    return NextResponse.json(await apiGetFresh<Slate>(`/slates/${date}`), {
      headers: { "Cache-Control": "no-store" },
    });
  } catch (error) {
    const status = error instanceof ApiError ? error.status : 502;
    return NextResponse.json({ detail: "Can't reach the API" }, { status });
  }
}
