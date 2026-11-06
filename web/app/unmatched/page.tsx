import { UnmatchedCard } from "@/components/UnmatchedCard";
import { Empty, PageTitle } from "@/components/ui";
import { apiGetFresh } from "@/lib/api";
import type { Team, UnmatchedItem } from "@/lib/types";

export const dynamic = "force-dynamic";

export default async function UnmatchedPage() {
  const items = await apiGetFresh<UnmatchedItem[]>("/unmatched");
  const leagues = [...new Set(items.map((item) => item.league))];
  const teams = Object.fromEntries(
    await Promise.all(
      leagues.map(
        async (league) => [league, await apiGetFresh<Team[]>(`/teams/${league}`)] as const,
      ),
    ),
  );
  return (
    <>
      <PageTitle
        title="Unmatched"
        sub="These games were left out rather than guessed. Confirm a match once, and the name matches from then on."
      />
      {items.length ? (
        <div className="space-y-4">
          {items.map((item) => (
            <UnmatchedCard key={item.event_id} item={item} teams={teams[item.league] ?? []} />
          ))}
        </div>
      ) : (
        <Empty title="Nothing to match">
          Every SportyBet event on recent slates found its game.
        </Empty>
      )}
    </>
  );
}
