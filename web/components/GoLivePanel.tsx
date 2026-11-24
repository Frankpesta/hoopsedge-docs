import { Card, StatusChip } from "@/components/ui";
import { lagosDateTime } from "@/lib/format";
import type { GoLive } from "@/lib/types";

function Step({
  number,
  title,
  done,
  children,
}: {
  number: number;
  title: string;
  done: boolean;
  children: React.ReactNode;
}) {
  return (
    <li className="border-line border-b pb-3 last:border-b-0 last:pb-0">
      <div className="flex items-start justify-between gap-2">
        <h3 className="text-sm font-semibold">
          {number}. {title}
        </h3>
        <StatusChip status={done ? "succeeded" : "none"} label={done ? "Done" : "Not yet"} />
      </div>
      <div className="mt-2 text-sm">{children}</div>
    </li>
  );
}

/** Brief §12 phase 8 (B7): where each go-live step stands. Read-only; the switch stays the owner's. */
export function GoLivePanel({
  golive,
  scheduledDryRun,
}: {
  golive: GoLive;
  scheduledDryRun: boolean;
}) {
  return (
    <Card>
      <ol className="space-y-3">
        <Step
          number={1}
          title={`${golive.days_needed} clean practice days`}
          done={golive.practice_clean}
        >
          <p className="text-text-2 text-xs">
            Every sport ran on schedule and succeeded, no job failed and no CRITICAL alert, on each
            of the last {golive.days_needed} full Lagos days.
          </p>
          <ul className="mt-2 space-y-2">
            {golive.days.map((day) => (
              <li key={day.day}>
                <div className="flex items-center justify-between gap-2">
                  <span className="num">{day.day}</span>
                  <StatusChip
                    status={day.clean ? "succeeded" : "failed"}
                    label={day.clean ? "Clean" : "Not clean"}
                  />
                </div>
                <p className="text-muted num text-xs">
                  {Object.entries(day.sports)
                    .map(
                      ([sport, s]) =>
                        `${sport} ${s.runs - s.failed}/${s.runs} runs, ${s.tickets} tickets`,
                    )
                    .join(" · ")}
                </p>
                {day.problems.length > 0 && (
                  <ul className="text-loss mt-1 list-disc pl-5 text-xs">
                    {day.problems.map((problem) => (
                      <li key={problem}>{problem}</li>
                    ))}
                  </ul>
                )}
              </li>
            ))}
          </ul>
        </Step>
        <Step number={2} title="One supervised real code per sport" done={golive.live_done}>
          <p className="text-text-2 text-xs">
            Turn Dry run off in Settings, press Run now with the owner watching, and the code must
            read back exactly as booked.
          </p>
          <ul className="mt-2 space-y-1">
            {Object.entries(golive.live_codes).map(([sport, code]) => (
              <li key={sport} className="flex justify-between gap-3">
                <span className="text-text-2">{sport}</span>
                <span className="num">
                  {code
                    ? `${code.code} · ${code.booked_at ? lagosDateTime(code.booked_at) : code.slate}`
                    : "Not yet"}
                </span>
              </li>
            ))}
          </ul>
        </Step>
        <Step number={3} title="Scheduled runs book real codes" done={!scheduledDryRun}>
          <p className="text-text-2 text-xs">
            {scheduledDryRun
              ? "Your switch, once steps 1 and 2 are done: set HOOPSEDGE_SCHEDULED_DRY_RUN=false in the server's .env, then recreate scheduler and api."
              : "Scheduled runs book real codes."}
          </p>
        </Step>
      </ol>
    </Card>
  );
}
