import { SlatePage } from "@/components/SlatePage";
import { easternDate } from "@/lib/format";

export const dynamic = "force-dynamic";

export default function TodayPage() {
  return <SlatePage date={easternDate()} today />;
}
