"use client";

import { useEffect, useRef, useState, useTransition } from "react";

import { addAlias, deleteAlias } from "@/lib/actions";
import type { AliasView, Sport, Team } from "@/lib/types";

const INPUT = "bg-surface-2 border-line min-h-11 w-full rounded-md border px-2 text-sm";

/** SportyBet name to ESPN team, per sport (D89). Seeds live in code; yours can be removed. */
export function AliasManager({
  sport,
  aliases,
  teams,
}: {
  sport: Sport;
  aliases: readonly AliasView[];
  teams: readonly Team[];
}) {
  const [name, setName] = useState("");
  const [search, setSearch] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const [pending, start] = useTransition();
  const [list, setList] = useState<readonly AliasView[]>(aliases);
  const key = useRef<string | null>(null);
  const chosen = teams.find((team) => team.name === search);
  useEffect(() => setList(aliases), [aliases]);

  /** The list follows each answer at once, rather than waiting for the page to reload. */
  function run<T>(
    work: (idempotency: string) => Promise<{ ok: true; data: T } | { ok: false; message: string }>,
    done: (data: T) => void,
  ) {
    key.current ??= crypto.randomUUID();
    start(async () => {
      const result = await work(key.current as string);
      key.current = null;
      if (result.ok) {
        setMessage(null);
        done(result.data);
      } else {
        setMessage(result.message);
      }
    });
  }

  return (
    <div>
      <form
        className="bg-surface border-line space-y-2 rounded-lg border p-4"
        onSubmit={(event) => {
          event.preventDefault();
          if (!chosen) return;
          run(
            (idempotency) => addAlias(sport, name, chosen.team_id, idempotency),
            (added) => {
              setList((current) => [...current.filter((a) => a.alias !== added.alias), added]);
              setName("");
              setSearch("");
            },
          );
        }}
      >
        <label className="text-text-2 block text-xs">
          SportyBet name, exactly as SportyBet writes it
          <input
            value={name}
            onChange={(event) => setName(event.target.value)}
            className={`${INPUT} mt-1`}
          />
        </label>
        <label className="text-text-2 block text-xs">
          ESPN team
          <input
            list={`teams-${sport}`}
            value={search}
            placeholder="Search every team"
            onChange={(event) => setSearch(event.target.value)}
            className={`${INPUT} mt-1`}
          />
        </label>
        <datalist id={`teams-${sport}`}>
          {teams.map((team) => (
            <option key={team.team_id} value={team.name}>
              {team.level}
            </option>
          ))}
        </datalist>
        {message && (
          <p role="alert" className="text-loss text-sm">
            {message}
          </p>
        )}
        <button
          type="submit"
          disabled={pending || name.trim().length < 2 || !chosen}
          className="bg-amber text-on-amber min-h-11 w-full rounded-md text-sm font-semibold disabled:opacity-50"
        >
          {pending ? "Saving" : "Add alias"}
        </button>
      </form>

      <ul className="divide-line mt-4 divide-y text-sm">
        {list.map((alias) => (
          <li
            key={`${alias.source}-${alias.alias}`}
            className="flex items-center justify-between gap-2 py-2"
          >
            <span>
              <span className="font-medium">{alias.alias}</span>
              <span className="text-text-2 block text-xs">
                {alias.team_name ?? alias.team_id} ·{" "}
                {alias.source === "seed" ? "built in" : "yours"}
              </span>
            </span>
            {alias.source === "owner" && (
              <button
                type="button"
                disabled={pending}
                onClick={() =>
                  run(
                    (idempotency) => deleteAlias(sport, alias.alias, idempotency),
                    () =>
                      setList((current) =>
                        current.filter((a) => !(a.source === "owner" && a.alias === alias.alias)),
                      ),
                  )
                }
                className="border-line min-h-11 rounded-md border px-3 text-xs"
              >
                Remove
              </button>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}
