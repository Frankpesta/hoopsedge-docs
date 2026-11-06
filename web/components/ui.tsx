import type { ReactNode } from "react";

import { statusLabel } from "@/lib/format";

export function PageTitle({ title, sub }: { title: string; sub?: ReactNode }) {
  return (
    <div className="mb-4">
      <h1 className="font-display text-4xl font-extrabold tracking-wide uppercase">{title}</h1>
      {sub && <p className="text-text-2 mt-1 text-sm">{sub}</p>}
    </div>
  );
}

export function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="mt-8">
      <h2 className="court-rule font-display mb-3 pb-2 pl-7 text-xl font-semibold tracking-wide uppercase">
        {title}
      </h2>
      {children}
    </section>
  );
}

export function Card({ children, className = "" }: { children: ReactNode; className?: string }) {
  return (
    <div className={`bg-surface border-line rounded-lg border p-4 ${className}`}>{children}</div>
  );
}

/** An empty state says why, so it can't be mistaken for an error (docs/10). */
export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="border-line rounded-lg border border-dashed p-6 text-center">
      <p className="font-semibold">{title}</p>
      {children && <p className="text-text-2 mt-1 text-sm">{children}</p>}
    </div>
  );
}

const BAD = new Set(["invalid", "failed", "lost", "rejected"]);
const GOOD = new Set(["verified", "succeeded", "won", "promoted"]);
const ICONS: Record<string, string> = { good: "✓", bad: "✕", plain: "•" };

/** Never colour alone: every state carries an icon and a word. */
export function StatusChip({ status, label }: { status: string; label?: string }) {
  const kind = BAD.has(status) ? "bad" : GOOD.has(status) ? "good" : "plain";
  const tone =
    kind === "bad"
      ? "border-loss text-loss"
      : kind === "good"
        ? "border-amber text-amber"
        : "border-line text-text-2";
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-medium ${tone}`}
    >
      <span aria-hidden>{ICONS[kind]}</span>
      {label ?? statusLabel(status)}
    </span>
  );
}

export function StatTile({
  label,
  value,
  note,
}: {
  label: string;
  value: ReactNode;
  note?: ReactNode;
}) {
  return (
    <div className="bg-surface border-line rounded-lg border p-3">
      <p className="text-text-2 text-xs">{label}</p>
      <p className="mt-1 text-2xl font-semibold">{value}</p>
      {note && <p className="text-muted mt-0.5 text-xs">{note}</p>}
    </div>
  );
}

/** Shot-clock segments: one lit block per 1/24 of the chance, with the fair price marked. */
export function ShotClock({ value, fair }: { value: number; fair?: number | null }) {
  const segments = 24;
  const lit = Math.round(value * segments);
  const mark = fair === null || fair === undefined ? null : Math.round(fair * segments);
  return (
    <div className="flex gap-px" aria-hidden>
      {Array.from({ length: segments }, (_, index) => (
        <span
          key={index}
          className={`h-2 flex-1 rounded-[1px] ${index < lit ? "bg-amber" : "bg-surface-2"} ${
            mark !== null && index === mark ? "outline-text outline-1" : ""
          }`}
        />
      ))}
    </div>
  );
}
