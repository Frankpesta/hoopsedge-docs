import { notFound } from "next/navigation";

import { SlatePage } from "@/components/SlatePage";
import { easternDate, isValidDate } from "@/lib/format";

export default async function SlateDatePage({ params }: { params: Promise<{ date: string }> }) {
  const { date } = await params;
  if (!isValidDate(date)) notFound();
  return <SlatePage date={date} today={date === easternDate()} />;
}
