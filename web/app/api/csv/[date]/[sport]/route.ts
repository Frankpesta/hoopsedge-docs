import { NextResponse } from "next/server";

import { ApiError, apiGetText } from "@/lib/api";
import { isValidDate } from "@/lib/format";
import { isSport } from "@/lib/types";

/** The owner's sheet as a file (brief §8.1). The key stays here on the server. */
export async function GET(
  _: Request,
  context: { params: Promise<{ date: string; sport: string }> },
) {
  const { date, sport } = await context.params;
  if (!isValidDate(date) || !isSport(sport)) {
    return NextResponse.json({ detail: "Bad date or sport" }, { status: 422 });
  }
  try {
    const found = await apiGetText(`/slates/${date}/${sport}/csv`);
    if (!found) return NextResponse.json({ detail: "No CSV for this slate" }, { status: 404 });
    return new NextResponse(found.text, {
      headers: {
        "Content-Type": "text/csv; charset=utf-8",
        "Content-Disposition": `attachment; filename="${found.filename}"`,
        "Cache-Control": "no-store",
      },
    });
  } catch (error) {
    const status = error instanceof ApiError ? error.status : 502;
    return NextResponse.json({ detail: "Can't reach the API" }, { status });
  }
}
