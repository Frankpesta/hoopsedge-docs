import Link from "next/link";

import { AliasManager } from "@/components/AliasManager";
import { UnmatchedQueue } from "@/components/UnmatchedQueue";
import { PageTitle, Section } from "@/components/ui";
import { apiGetFresh } from "@/lib/api";
import { SPORTS, isSport } from "@/lib/types";
import type { AliasView, Sport, Team, UnmatchedItem } from "@/lib/types";

export const dynamic = "force-dynamic";

export default async function TeamsPage({
  searchParams,
}: {
  searchParams: Promise<{ sport?: string }>;
}) {
  const { sport: asked } = await searchParams;
  const sport: Sport = isSport(asked) ? asked : "NCAAB";
  const items = await apiGetFresh<UnmatchedItem[]>("/unmatched");
  const needed = [...new Set<Sport>([...items.map((item) => item.sport), sport])];
  const [teamLists, aliases] = await Promise.all([
    Promise.all(
      needed.map(async (name) => [name, await apiGetFresh<Team[]>(`/teams/${name}`)] as const),
    ),
    apiGetFresh<AliasView[]>(`/aliases/${sport}`),
  ]);
  const teams = Object.fromEntries(teamLists) as Partial<Record<Sport, Team[]>>;
  return (
    <>
      <PageTitle
        title="Teams"
        sub="Games whose team names didn't match exactly were left out, never guessed. Match a name once and it matches from then on."
      />
      <Section title={`Review queue (${items.length})`}>
        <UnmatchedQueue items={items} teams={teams} />
      </Section>
      <Section title="Aliases">
        <nav aria-label="Sport" className="mb-3 flex gap-2">
          {SPORTS.map((name) => (
            <Link
              key={name}
              href={`/unmatched?sport=${name}`}
              aria-current={name === sport ? "page" : undefined}
              className={`font-display min-h-11 rounded-md px-4 py-2 text-lg font-semibold uppercase ${
                name === sport ? "bg-surface-2 text-text" : "text-muted"
              }`}
            >
              {name}
            </Link>
          ))}
        </nav>
        <AliasManager sport={sport} aliases={aliases} teams={teams[sport] ?? []} />
      </Section>
    </>
  );
}
