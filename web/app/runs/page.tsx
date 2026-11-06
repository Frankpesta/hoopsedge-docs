import Link from "next/link";

import { Empty, PageTitle, StatusChip } from "@/components/ui";
import { apiGet } from "@/lib/api";
import { lagosDateTime, secondsBetween } from "@/lib/format";
import type { RunSummary } from "@/lib/types";

export default async function RunsPage() {
  const runs = await apiGet<RunSummary[]>("/runs?limit=50");
  return (
    <>
      <PageTitle title="Runs" sub="Every pipeline run, newest first." />
      {runs.length ? (
        <ul className="space-y-2">
          {runs.map((run) => {
            const seconds = secondsBetween(run.started_at, run.ended_at);
            return (
              <li key={run.run_id}>
                <Link
                  href={`/runs/${run.run_id}`}
                  className="bg-surface border-line block rounded-md border p-3"
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-semibold">
                      {run.league} · {run.slate_date}
                    </span>
                    <StatusChip status={run.status} />
                  </div>
                  <p className="text-text-2 mt-1 text-sm">
                    {lagosDateTime(run.started_at)}
                    {seconds === null ? "" : ` · ${seconds.toFixed(0)} s`}
                    {run.trigger ? ` · ${run.trigger}` : ""}
                    {run.dry_run ? " · dry run" : ""}
                  </p>
                  {run.error && <p className="text-loss mt-1 text-sm">{run.error}</p>}
                </Link>
              </li>
            );
          })}
        </ul>
      ) : (
        <Empty title="No runs yet">Runs appear here once a slate has been started.</Empty>
      )}
    </>
  );
}
