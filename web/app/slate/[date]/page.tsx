import { notFound } from "next/navigation";

import { SlatePage } from "@/components/SlatePage";
import { easternDate, isValidDate } from "@/lib/format";
import { isSport } from "@/lib/types";

export default async function SlateDatePage({
  params,
  searchParams,
}: {
  params: Promise<{ date: string }>;
  searchParams: Promise<{ sport?: string }>;
}) {
  const { date } = await params;
  const { sport } = await searchParams;
  if (!isValidDate(date)) notFound();
  return (
    <SlatePage
      date={date}
      today={date === easternDate()}
      sport={isSport(sport) ? sport : "NCAAB"}
    />
  );
}
