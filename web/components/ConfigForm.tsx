"use client";

import { type ReactNode, useMemo, useRef, useState, useTransition } from "react";

import { saveConfig } from "@/lib/actions";
import { KIND_NAMES, MODE_LABELS, diffSettings } from "@/lib/format";
import { SPORTS } from "@/lib/types";
import type { ConfigDocument, SportConfig } from "@/lib/types";

const INPUT = "bg-surface-2 border-line text-text min-h-11 w-full rounded-md border px-2 text-sm";
const SCHEDULE_LABELS: Record<string, string> = {
  ncaab_slate: "NCAAB slate",
  ncaab_late_slate: "NCAAB late slate",
  nba_slate: "NBA slate",
  nba_late_slate: "NBA late slate",
  ncaaf_slate: "NCAAF slate",
  ncaaf_saturday_slate: "NCAAF Saturday",
  ncaaf_scores: "NCAAF scores",
  ncaab_scores: "NCAAB scores",
  nba_scores: "NBA scores",
  backup: "Database backup",
};

function Toggle({
  label,
  checked,
  onChange,
  note,
}: {
  label: string;
  checked: boolean;
  onChange: (value: boolean) => void;
  note?: string;
}) {
  return (
    <label className="flex min-h-11 items-center justify-between gap-3">
      <span>
        <span className="text-sm">{label}</span>
        {note && <span className="text-muted block text-xs">{note}</span>}
      </span>
      <input
        type="checkbox"
        checked={checked}
        onChange={(event) => onChange(event.target.checked)}
        className="accent-amber h-5 w-5 shrink-0"
      />
    </label>
  );
}

function NumberField({
  label,
  value,
  onChange,
  step = 0.5,
  min,
}: {
  label: string;
  value: number;
  onChange: (value: number) => void;
  step?: number;
  min?: number;
}) {
  return (
    <label className="text-text-2 text-xs">
      {label}
      <input
        type="number"
        step={step}
        min={min}
        value={Number.isFinite(value) ? value : ""}
        onChange={(event) => onChange(Number(event.target.value))}
        className={`${INPUT} num mt-1`}
      />
    </label>
  );
}

function Group({ title, children }: { title: string; children: ReactNode }) {
  return (
    <fieldset className="bg-surface border-line rounded-lg border p-4">
      <legend className="font-display px-1 text-lg font-semibold uppercase">{title}</legend>
      {children}
    </fieldset>
  );
}

function SportFields({
  sport,
  config,
  edit,
}: {
  sport: string;
  config: SportConfig;
  edit: (change: (next: SportConfig) => void) => void;
}) {
  const tiers = typeof config.tiers === "string" ? null : config.tiers;
  return (
    <Group title={sport}>
      <Toggle
        label={`${sport} on`}
        checked={config.enabled}
        onChange={(v) => edit((c) => void (c.enabled = v))}
      />
      <div className="mt-2 grid grid-cols-3 items-end gap-2">
        <NumberField
          label="Over at edge ≥"
          value={config.over_threshold}
          min={0}
          onChange={(v) => edit((c) => void (c.over_threshold = v))}
        />
        <NumberField
          label="Under at edge ≤ −"
          value={config.under_threshold}
          min={0}
          onChange={(v) => edit((c) => void (c.under_threshold = v))}
        />
        <NumberField
          label="Team total edge ≥"
          value={config.team_total_threshold}
          min={0}
          onChange={(v) => edit((c) => void (c.team_total_threshold = v))}
        />
        <NumberField
          label="Last N games"
          value={config.l5_window}
          step={1}
          min={1}
          onChange={(v) => edit((c) => void (c.l5_window = v))}
        />
        <NumberField
          label="Fewest games"
          value={config.min_games}
          step={1}
          min={1}
          onChange={(v) => edit((c) => void (c.min_games = v))}
        />
        <NumberField
          label="Cushion (points)"
          value={config.min_cushion}
          min={0}
          onChange={(v) => edit((c) => void (c.min_cushion = v))}
        />
      </div>
      <div className="mt-2 grid grid-cols-2 gap-2">
        {(["totals", "team_totals"] as const).map((market) => (
          <label key={market} className="text-text-2 text-xs">
            {market === "totals" ? "Totals line" : "Team totals line"}
            <select
              value={config.line_mode[market]}
              onChange={(event) => edit((c) => void (c.line_mode[market] = event.target.value))}
              className={`${INPUT} mt-1`}
            >
              {Object.entries(MODE_LABELS).map(([mode, label]) => (
                <option key={mode} value={mode}>
                  {label}
                </option>
              ))}
            </select>
          </label>
        ))}
      </div>
      <Toggle
        label="Only same-level opponents in the window"
        note="Off matches the hand-run model. Both variants are kept for grading."
        checked={config.l5_same_level_only}
        onChange={(v) => edit((c) => void (c.l5_same_level_only = v))}
      />
      {tiers ? (
        <div className="mt-2 grid grid-cols-4 items-end gap-2">
          <NumberField
            label="Attack low <"
            value={tiers.atk[0]}
            onChange={(v) => edit((c) => void ((c.tiers as typeof tiers).atk[0] = v))}
          />
          <NumberField
            label="Attack high ≥"
            value={tiers.atk[1]}
            onChange={(v) => edit((c) => void ((c.tiers as typeof tiers).atk[1] = v))}
          />
          <NumberField
            label="Defence high ≤"
            value={tiers.def[0]}
            onChange={(v) => edit((c) => void ((c.tiers as typeof tiers).def[0] = v))}
          />
          <NumberField
            label="Defence low ≥"
            value={tiers.def[1]}
            onChange={(v) => edit((c) => void ((c.tiers as typeof tiers).def[1] = v))}
          />
        </div>
      ) : (
        <p className="text-muted mt-2 text-xs">
          Tiers are provisional: each slate splits the league into thirds by its teams&apos; last-5
          averages.
        </p>
      )}
    </Group>
  );
}

export function ConfigForm({
  version: loadedVersion,
  document: loaded,
}: {
  version: number;
  document: ConfigDocument;
}) {
  // The last saved version comes from each save's answer, so a second save needs no reload.
  const [base, setBase] = useState({ version: loadedVersion, document: loaded });
  const saved = base.document;
  const [draft, setDraft] = useState<ConfigDocument>(() => structuredClone(loaded));
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);
  const [pending, start] = useTransition();
  const key = useRef<string | null>(null);
  const changes = useMemo(() => diffSettings(saved, draft), [saved, draft]);

  function update(change: (next: ConfigDocument) => void) {
    setMessage(null);
    setDraft((current) => {
      const next = structuredClone(current);
      change(next);
      return next;
    });
  }

  function save() {
    key.current ??= crypto.randomUUID();
    start(async () => {
      const result = await saveConfig(base.version, draft, key.current as string);
      key.current = null;
      if (result.ok) {
        const document = result.data.document as unknown as ConfigDocument;
        setBase({ version: result.data.version, document });
        setDraft(structuredClone(document));
        setMessage({
          ok: true,
          text: `Saved as version ${result.data.version}. The next run uses it.`,
        });
      } else {
        setMessage({ ok: false, text: result.message });
      }
    });
  }

  return (
    <form
      className="space-y-4"
      onSubmit={(event) => {
        event.preventDefault();
        save();
      }}
    >
      <Group title="Safety">
        <Toggle
          label="Dry run"
          note="On: every code is a practice code. Off: Run now books real codes; scheduled runs also need the server's switch."
          checked={draft.dry_run}
          onChange={(v) => update((next) => void (next.dry_run = v))}
        />
        <Toggle
          label="Pause all scheduled runs"
          note="Run now still works."
          checked={draft.pause_all}
          onChange={(v) => update((next) => void (next.pause_all = v))}
        />
        <Toggle
          label="Route SportyBet through the proxy"
          checked={draft.sportybet_proxy_enabled}
          onChange={(v) => update((next) => void (next.sportybet_proxy_enabled = v))}
        />
      </Group>

      {SPORTS.map((sport) => (
        <SportFields
          key={sport}
          sport={sport}
          config={draft.sports[sport]}
          edit={(change) => update((next) => change(next.sports[sport]))}
        />
      ))}

      <Group title="Tickets">
        <div className="grid grid-cols-2 items-end gap-2">
          <NumberField
            label="Most legs on a ticket"
            value={draft.tickets.max_legs}
            step={1}
            min={1}
            onChange={(v) => update((n) => void (n.tickets.max_legs = v))}
          />
          <NumberField
            label="Fewest legs on a ticket"
            value={draft.tickets.min_legs}
            step={1}
            min={1}
            onChange={(v) => update((n) => void (n.tickets.min_legs = v))}
          />
          <NumberField
            label="Top games per kind"
            value={draft.tickets.max_games_per_kind}
            step={1}
            min={1}
            onChange={(v) => update((n) => void (n.tickets.max_games_per_kind = v))}
          />
          <NumberField
            label="Stop booking, minutes before kickoff"
            value={draft.tickets.booking_cutoff_minutes}
            step={1}
            min={20}
            onChange={(v) => update((n) => void (n.tickets.booking_cutoff_minutes = v))}
          />
        </div>
        {Object.keys(draft.tickets.kinds).map((kind) => (
          <Toggle
            key={kind}
            label={KIND_NAMES[kind] ?? kind}
            checked={draft.tickets.kinds[kind] ?? false}
            onChange={(v) => update((n) => void (n.tickets.kinds[kind] = v))}
          />
        ))}
      </Group>

      <Group title="Outlook">
        <div className="grid grid-cols-2 items-end gap-2">
          <NumberField
            label="LOW up to (score 0–8)"
            value={draft.outlook.low_max}
            step={1}
            min={0}
            onChange={(v) => update((n) => void (n.outlook.low_max = v))}
          />
          <NumberField
            label="MEDIUM up to"
            value={draft.outlook.medium_max}
            step={1}
            min={0}
            onChange={(v) => update((n) => void (n.outlook.medium_max = v))}
          />
        </div>
        <p className="text-muted mt-2 text-xs">
          The outlook is information only: it never changes a lean.
        </p>
      </Group>

      <Group title="Schedules (Lagos)">
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
          {Object.keys(draft.schedules).map((name) => (
            <label key={name} className="text-text-2 text-xs">
              {SCHEDULE_LABELS[name] ?? name}
              <input
                type="time"
                value={draft.schedules[name] ?? ""}
                onChange={(event) => update((n) => void (n.schedules[name] = event.target.value))}
                className={`${INPUT} mt-1`}
              />
            </label>
          ))}
        </div>
      </Group>

      <div className="bg-surface border-line sticky bottom-16 rounded-lg border p-4">
        <p className="font-semibold">
          {changes.length ? `${changes.length} unsaved changes` : "No unsaved changes"}
        </p>
        {changes.length > 0 && (
          <ul className="num text-text-2 mt-1 max-h-32 space-y-0.5 overflow-y-auto text-xs">
            {changes.map((change) => (
              <li key={change.path} className="break-all">
                {change.path}: {String(change.from)} {"→"} {String(change.to)}
              </li>
            ))}
          </ul>
        )}
        {message && (
          <p role="status" className={`mt-2 text-sm ${message.ok ? "" : "text-loss"}`}>
            {message.text}
          </p>
        )}
        <button
          type="submit"
          disabled={pending || !changes.length}
          className="bg-amber text-on-amber mt-2 min-h-11 w-full rounded-md font-semibold disabled:opacity-50"
        >
          {pending ? "Saving" : "Save as a new version"}
        </button>
      </div>
    </form>
  );
}
