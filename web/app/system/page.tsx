import { Card, Empty, PageTitle, Section, StatTile, StatusChip } from "@/components/ui";
import { apiGetFresh } from "@/lib/api";
import { lagosDateTime } from "@/lib/format";
import type { SystemView } from "@/lib/types";

type Alert = { sk?: string; severity?: string; message?: string; ref?: string };
type Cost = { mtd?: number; forecast?: number };

const FRESHNESS: Record<string, string> = {
  last_scheduled_job: "Last scheduled job",
  ncaaf_scores: "NCAAF scores refreshed",
  ncaab_scores: "NCAAB scores refreshed",
  nba_scores: "NBA scores refreshed",
  ncaaf_run: "Last NCAAF run",
  ncaab_run: "Last NCAAB run",
  nba_run: "Last NBA run",
};

export const dynamic = "force-dynamic";

export default async function SystemPage() {
  const system = await apiGetFresh<SystemView>("/system");
  const relay = system.relay;
  const cost = system.cost as Cost | null;
  const alerts = system.alerts as Alert[];
  return (
    <>
      <PageTitle title="System" />
      <div className="grid grid-cols-2 gap-2">
        <StatTile
          label="Cost this month"
          value={cost?.mtd === undefined ? "n/a" : `$${cost.mtd.toFixed(2)}`}
          note={
            cost
              ? `Forecast $${(cost.forecast ?? 0).toFixed(2)}`
              : "Running locally, so there's no AWS cost yet"
          }
        />
        <StatTile
          label="Budget alerts"
          value={system.budget_usd.map((amount) => `$${amount}`).join(" · ")}
        />
      </div>

      <div className="mt-2 grid grid-cols-2 gap-2">
        <StatTile
          label="SportyBet route"
          value={relay.mode === "direct" ? "Direct" : relay.reachable ? "Relay up" : "Relay down"}
          note={
            relay.mode === "direct"
              ? "No proxy set: SportyBet is called directly"
              : `Checked ${lagosDateTime(relay.checked_at)}`
          }
        />
        <StatTile
          label="Codes"
          value={system.dry_run ? "Practice" : "Real"}
          note={system.scheduled_dry_run ? "Scheduled runs: practice" : "Scheduled runs: real"}
        />
      </div>

      <Section title="Data freshness">
        <Card>
          <ul className="space-y-1 text-sm">
            {Object.entries(system.freshness).map(([name, when]) => (
              <li key={name} className="flex justify-between gap-3">
                <span className="text-text-2">{FRESHNESS[name] ?? name}</span>
                <span className="num">{when ? lagosDateTime(when) : "Not yet"}</span>
              </li>
            ))}
          </ul>
        </Card>
      </Section>

      <Section title="Recent alerts">
        {alerts.length ? (
          <ul className="space-y-2">
            {alerts.map((alert, index) => (
              <li key={index} className="bg-surface border-line rounded-md border p-3 text-sm">
                <div className="flex items-center justify-between gap-2">
                  <StatusChip
                    status={alert.severity === "CRITICAL" ? "failed" : "none"}
                    label={alert.severity ?? "Alert"}
                  />
                  <span className="text-muted text-xs">
                    {lagosDateTime(String(alert.sk ?? "").split("#")[0])}
                  </span>
                </div>
                <p className="mt-1">{alert.message}</p>
              </li>
            ))}
          </ul>
        ) : (
          <Empty title="No alerts">Nothing has gone wrong that the system knows about.</Empty>
        )}
      </Section>
    </>
  );
}
