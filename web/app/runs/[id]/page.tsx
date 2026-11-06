import { notFound } from "next/navigation";

import { Card, Empty, PageTitle, Section, StatusChip } from "@/components/ui";
import { apiGetOptional } from "@/lib/api";
import { lagosDateTime, secondsBetween } from "@/lib/format";
import type { RunDetail } from "@/lib/types";

type Step = { name?: string; started?: string; ended?: string };
type Entry = { sk?: string; step?: string; event?: string; payload?: unknown };

export default async function RunPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const detail = await apiGetOptional<RunDetail>(`/runs/${encodeURIComponent(id)}`);
  if (!detail) notFound();
  const { run } = detail;
  const steps = detail.steps as Step[];
  const log = detail.log as Entry[];
  const longest = Math.max(
    1,
    ...steps.map((step) => secondsBetween(step.started ?? "", step.ended) ?? 0),
  );
  return (
    <>
      <PageTitle
        title={`${run.league} run`}
        sub={`Slate ${run.slate_date} · ${lagosDateTime(run.started_at)}`}
      />
      <StatusChip status={run.status} />
      {run.error && <p className="text-loss mt-2 text-sm">{run.error}</p>}
      <Section title="Steps">
        {steps.length ? (
          <Card>
            <ul className="space-y-2">
              {steps.map((step) => {
                const seconds = secondsBetween(step.started ?? "", step.ended) ?? 0;
                return (
                  <li key={step.name}>
                    <div className="flex justify-between text-sm">
                      <span>{step.name}</span>
                      <span className="num text-text-2">{seconds.toFixed(1)} s</span>
                    </div>
                    <span
                      aria-hidden
                      className="mt-1 block h-1 rounded-r-sm"
                      style={{
                        width: `${(seconds / longest) * 100}%`,
                        minWidth: 2,
                        background: "var(--series-1)",
                      }}
                    />
                  </li>
                );
              })}
            </ul>
          </Card>
        ) : (
          <Empty title="No steps recorded">This run stopped before its first step finished.</Empty>
        )}
      </Section>
      <Section title="Log">
        {log.length ? (
          <ul className="space-y-1">
            {log.map((entry, index) => (
              <li key={index} className="bg-surface border-line rounded-md border p-2 text-sm">
                <span className="font-semibold">{entry.event}</span>{" "}
                <span className="text-muted text-xs">{entry.step}</span>
                {entry.payload ? (
                  <pre className="num text-text-2 mt-1 overflow-x-auto text-xs">
                    {JSON.stringify(entry.payload)}
                  </pre>
                ) : null}
              </li>
            ))}
          </ul>
        ) : (
          <Empty title="Nothing logged">No legs were dropped or changed on this run.</Empty>
        )}
      </Section>
    </>
  );
}
