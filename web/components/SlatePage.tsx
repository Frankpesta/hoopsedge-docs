import Link from "next/link";

import { apiGetFresh } from "@/lib/api";
import { longDate, shiftDate } from "@/lib/format";
import type { Slate, Sport, SystemView } from "@/lib/types";

import { SlateBoard } from "./SlateBoard";
import { PageTitle } from "./ui";

export async function SlatePage({
  date,
  today,
  sport,
}: {
  date: string;
  today: boolean;
  sport: Sport;
}) {
  // Fresh: a cached "running" here would keep the board polling after the run has finished.
  const [slate, system] = await Promise.all([
    apiGetFresh<Slate>(`/slates/${date}`),
    apiGetFresh<SystemView>("/system"),
  ]);
  return (
    <>
      <PageTitle title={today ? "Today" : "Slate"} sub={`${longDate(date)} (US Eastern)`} />
      <nav aria-label="Dates" className="mb-3 flex justify-between text-sm">
        <Link
          href={`/slate/${shiftDate(date, -1)}?sport=${sport}`}
          className="text-text-2 min-h-8 underline"
        >
          {"←"} Previous day
        </Link>
        <Link
          href={`/slate/${shiftDate(date, 1)}?sport=${sport}`}
          className="text-text-2 min-h-8 underline"
        >
          Next day {"→"}
        </Link>
      </nav>
      <SlateBoard key={date} initial={slate} sport={sport} realCodes={!system.dry_run} />
    </>
  );
}
