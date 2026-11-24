import { SlatePage } from "@/components/SlatePage";
import { easternDate } from "@/lib/format";
import { isSport } from "@/lib/types";

export const dynamic = "force-dynamic";

export default async function TodayPage({
  searchParams,
}: {
  searchParams: Promise<{ sport?: string }>;
}) {
  const { sport } = await searchParams;
  return <SlatePage date={easternDate()} today sport={isSport(sport) ? sport : "NCAAB"} />;
}
