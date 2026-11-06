"use client";

import { useRouter } from "next/navigation";
import { useRef, useState, useTransition } from "react";

import { resolveUnmatched } from "@/lib/actions";
import { lagosDateTime } from "@/lib/format";
import type { Team, UnmatchedItem } from "@/lib/types";

const REASONS: Record<string, string> = {
  team_not_resolved: "A team name couldn't be matched with confidence",
  no_game_found: "No game on the schedule has these two teams at this time",
  several_games_found: "More than one game could be this one",
  same_team_twice: "Both names point to the same team",
  swapped_at_a_true_home_game: "SportyBet lists the teams the other way round at a home game",
};

function Side({
  label,
  name,
  suggestions,
  teams,
  value,
  onChange,
  listId,
}: {
  label: string;
  name: string;
  suggestions: readonly { team_id?: unknown }[];
  teams: readonly Team[];
  value: string;
  onChange: (team: string) => void;
  listId: string;
}) {
  const byId = new Map(teams.map((team) => [team.team_id, team.name]));
  const [search, setSearch] = useState("");
  return (
    <div className="mt-3">
      <p className="text-text-2 text-xs">
        {label}: <span className="text-text font-semibold">{name}</span>
      </p>
      <div className="mt-1 flex flex-wrap gap-1.5">
        {suggestions.map((item) => {
          const team = String(item.team_id);
          return (
            <button
              key={team}
              type="button"
              aria-pressed={value === team}
              onClick={() => {
                onChange(team);
                setSearch(byId.get(team) ?? "");
              }}
              className={`min-h-11 rounded-md border px-3 text-sm ${
                value === team ? "border-amber text-amber font-semibold" : "border-line"
              }`}
            >
              {byId.get(team) ?? team}
            </button>
          );
        })}
      </div>
      <input
        list={listId}
        value={search}
        placeholder="Or search every team"
        aria-label={`Search for ${name}`}
        onChange={(event) => {
          setSearch(event.target.value);
          const found = teams.find((team) => team.name === event.target.value);
          onChange(found?.team_id ?? "");
        }}
        className="bg-surface-2 border-line mt-2 min-h-11 w-full rounded-md border px-2 text-sm"
      />
      <datalist id={listId}>
        {teams.map((team) => (
          <option key={team.team_id} value={team.name} />
        ))}
      </datalist>
    </div>
  );
}

export function UnmatchedCard({ item, teams }: { item: UnmatchedItem; teams: readonly Team[] }) {
  const [home, setHome] = useState(item.home_team_id ?? "");
  const [away, setAway] = useState(item.away_team_id ?? "");
  const [message, setMessage] = useState<string | null>(null);
  const [pending, start] = useTransition();
  const key = useRef<string | null>(null);
  const router = useRouter();

  function send(choice: { team_id_home: string; team_id_away: string } | { ignore: true }) {
    // The same key until it succeeds, so a double tap can't confirm twice.
    key.current ??= crypto.randomUUID();
    start(async () => {
      const result = await resolveUnmatched(item.event_id, choice, key.current as string);
      key.current = null;
      if (result.ok) router.refresh();
      else setMessage(result.message);
    });
  }

  const safe = item.event_id.replace(/\W/g, "");
  return (
    <article className="bg-surface border-line rounded-lg border p-4">
      <h2 className="font-semibold">
        {item.away_name} at {item.home_name}
      </h2>
      <p className="text-text-2 mt-1 text-sm">
        {item.league} · {lagosDateTime(item.start)} Lagos
      </p>
      <p className="text-text-2 mt-1 text-sm">{REASONS[item.reason ?? ""] ?? item.reason}</p>
      <Side
        label="Home"
        name={item.home_name}
        suggestions={item.home_suggestions ?? []}
        teams={teams}
        value={home}
        onChange={setHome}
        listId={`${safe}-home`}
      />
      <Side
        label="Away"
        name={item.away_name}
        suggestions={item.away_suggestions ?? []}
        teams={teams}
        value={away}
        onChange={setAway}
        listId={`${safe}-away`}
      />
      {message && (
        <p role="alert" className="text-loss mt-2 text-sm">
          {message}
        </p>
      )}
      <div className="mt-4 flex justify-between gap-2">
        <button
          type="button"
          disabled={pending}
          onClick={() => send({ ignore: true })}
          className="border-line min-h-11 rounded-md border px-4 text-sm"
        >
          Ignore
        </button>
        <button
          type="button"
          disabled={pending || !home || !away}
          onClick={() => send({ team_id_home: home, team_id_away: away })}
          className="bg-amber text-on-amber min-h-11 rounded-md px-4 text-sm font-semibold disabled:opacity-50"
        >
          {pending ? "Saving" : "Confirm match"}
        </button>
      </div>
    </article>
  );
}
