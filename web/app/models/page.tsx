import { LearningCurve } from "@/components/charts/RecordCharts";
import { Rollback } from "@/components/Rollback";
import { Card, Empty, PageTitle, Section, StatusChip } from "@/components/ui";
import { apiGet } from "@/lib/api";
import { FAMILIES, FAMILY_NAMES, lagosDateTime } from "@/lib/format";
import type { ModelsView } from "@/lib/types";

type Gate = { passed: boolean; edge: string; reason: string; override?: unknown };
type Drift = { flags?: string[]; note?: string; games?: number };

function LeagueModels({ view }: { view: ModelsView }) {
  const champion = view.champion as {
    version?: string;
    promoted_at?: string;
    reason?: string;
  } | null;
  const gates = view.gates as Record<string, Gate>;
  const drift = view.drift as Drift | null;
  const curve = view.learning_curve.flatMap((point) =>
    point.log_likelihood_gain === null || point.log_likelihood_gain === undefined
      ? []
      : [
          {
            at: point.at,
            version: point.version,
            gain: point.log_likelihood_gain,
            decision: point.decision,
          },
        ],
  );
  const history = [...view.learning_curve].reverse();
  const versions = new Set(view.learning_curve.map((point) => point.version));
  if (champion?.version) versions.delete(champion.version);
  return (
    <Section title={view.league}>
      {champion ? (
        <Card>
          <p className="text-text-2 text-xs">Champion</p>
          <p className="num mt-1 font-semibold break-all">{champion.version}</p>
          <p className="text-text-2 mt-1 text-sm">{champion.reason}</p>
          <p className="text-muted mt-1 text-xs">Since {lagosDateTime(champion.promoted_at)}</p>
        </Card>
      ) : (
        <Empty title="No champion yet">Nothing can run for this league until one is set.</Empty>
      )}

      {drift?.flags && drift.flags.length > 0 && (
        <div role="alert" className="border-loss mt-3 rounded-lg border p-3 text-sm">
          <p className="text-loss font-semibold">
            <span aria-hidden>{"✕ "}</span>Drift warning
          </p>
          <ul className="mt-1">
            {drift.flags.map((flag) => (
              <li key={flag}>{flag}</li>
            ))}
          </ul>
        </div>
      )}

      <h3 className="mt-4 mb-2 font-semibold">Does it beat the baseline?</h3>
      <ul className="space-y-2">
        {FAMILIES.map((family) => {
          const gate = gates[family];
          if (!gate) return null;
          return (
            <li key={family} className="bg-surface border-line rounded-md border p-3 text-sm">
              <div className="flex items-center justify-between gap-2">
                <span className="font-semibold">{FAMILY_NAMES[family]}</span>
                <StatusChip
                  status={gate.passed ? "verified" : "failed"}
                  label={gate.passed ? "Beats it" : "Doesn't beat it"}
                />
              </div>
              <p className="text-text-2 mt-1 text-xs">
                {gate.edge === "unproven"
                  ? "Edge over the market: unproven, because history has no real lines for it."
                  : "Judged on real market lines."}
                {gate.override ? " Kept on by your decision." : ""}
              </p>
            </li>
          );
        })}
      </ul>

      {curve.length > 0 && (
        <div className="mt-4">
          <LearningCurve points={curve} />
        </div>
      )}

      <h3 className="mt-4 mb-2 font-semibold">Decisions</h3>
      {history.length ? (
        <ul className="space-y-2">
          {history.map((point, index) => (
            <li key={index} className="bg-surface border-line rounded-md border p-3 text-sm">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <StatusChip status={point.decision} label={point.decision} />
                <span className="text-muted text-xs">{lagosDateTime(point.at)}</span>
              </div>
              <p className="num text-text-2 mt-1 text-xs break-all">{point.version}</p>
              {point.reasons.map((reason) => (
                <p key={reason} className="mt-1">
                  {reason}
                </p>
              ))}
            </li>
          ))}
        </ul>
      ) : (
        <Empty title="No decisions yet" />
      )}

      {versions.size > 0 && (
        <>
          <h3 className="mt-4 mb-2 font-semibold">Earlier versions</h3>
          <ul className="space-y-2">
            {[...versions].map((version) => (
              <li
                key={version}
                className="bg-surface border-line flex flex-wrap items-center justify-between gap-2 rounded-md border p-3"
              >
                <span className="num text-sm break-all">{version}</span>
                <Rollback league={view.league} version={version} />
              </li>
            ))}
          </ul>
        </>
      )}
    </Section>
  );
}

export default async function ModelsPage() {
  const [nba, ncaa] = await Promise.all([
    apiGet<ModelsView>("/models/NBA"),
    apiGet<ModelsView>("/models/NCAA"),
  ]);
  return (
    <>
      <PageTitle
        title="Model lab"
        sub="A new model only takes over if it's measurably better on games neither trained on."
      />
      <LeagueModels view={nba} />
      <LeagueModels view={ncaa} />
    </>
  );
}
