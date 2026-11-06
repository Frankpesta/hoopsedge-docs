import Link from "next/link";
import { notFound } from "next/navigation";

import { DistributionChart } from "@/components/charts/DistributionChart";
import { Card, Empty, PageTitle, Section, StatusChip } from "@/components/ui";
import { apiGetOptional } from "@/lib/api";
import { lagosTime, outcomeLabel, percent, reasonLabel, signedPercent } from "@/lib/format";
import type { GameDetail } from "@/lib/types";

type Period = {
  mu_home: number;
  mu_away: number;
  sd_margin: number;
  sd_total: number;
};

type Row = Record<string, unknown>;

function lineFor(game: GameDetail, family: string): number | null {
  const found = (game.lines as Row[]).find((row) => row.family === family);
  return typeof found?.line === "number" ? found.line : null;
}

const RATINGS: readonly { name: string; home: string; away: string }[] = [
  { name: "Offence", home: "adj_o_h", away: "adj_o_a" },
  { name: "Defence (points allowed)", home: "adj_d_h", away: "adj_d_a" },
  { name: "Tempo", home: "adj_t_h", away: "adj_t_a" },
  { name: "Elo", home: "elo_h", away: "elo_a" },
  { name: "Recent form", home: "form_h", away: "form_a" },
  { name: "Rest (days)", home: "rest_h", away: "rest_a" },
  { name: "Games played", home: "games_h", away: "games_a" },
];

function RatingRow({
  name,
  home,
  away,
}: {
  name: string;
  home: number | null | undefined;
  away: number | null | undefined;
}) {
  const show = (value: number | null | undefined) =>
    value === null || value === undefined ? "n/a" : value.toFixed(1);
  const homeSize = Math.abs(home ?? 0);
  const awaySize = Math.abs(away ?? 0);
  const largest = Math.max(homeSize, awaySize, 1e-6);
  return (
    <li className="border-line border-t py-2 first:border-t-0">
      <p className="text-text-2 text-center text-xs">{name}</p>
      <div className="mt-1 grid grid-cols-[3.25rem_1fr_1fr_3.25rem] items-center gap-1.5 text-sm">
        <span className="num text-right">{show(home)}</span>
        <span className="flex justify-end" aria-hidden>
          <span
            className="block h-1.5 rounded-l-sm"
            style={{ width: `${(homeSize / largest) * 100}%`, background: "var(--series-1)" }}
          />
        </span>
        <span aria-hidden>
          <span
            className="block h-1.5 rounded-r-sm"
            style={{ width: `${(awaySize / largest) * 100}%`, background: "var(--series-2)" }}
          />
        </span>
        <span className="num">{show(away)}</span>
      </div>
    </li>
  );
}

function ScoredTable({ game }: { game: GameDetail }) {
  const labels = game.edge_labels;
  const legs = (game.legs as Row[]).map((leg) => ({ ...leg, used: true }));
  const rows: Row[] = [...legs, ...(game.rejections as Row[])];
  if (!rows.length) {
    return <Empty title="No outcomes scored yet">This game hasn&apos;t been in a run.</Empty>;
  }
  return (
    <ul className="space-y-2">
      {rows.map((row, index) => {
        const family = String(row.family);
        const unproven = labels[family] === "unproven";
        return (
          <li
            key={index}
            className="bg-surface border-line flex flex-wrap items-center justify-between gap-2 rounded-md border p-3 text-sm"
          >
            <span className="font-semibold">
              {outcomeLabel(
                row,
                game.lines as Row[],
                game.home_name ?? "Home",
                game.away_name ?? "Away",
              )}
            </span>
            <span className="text-text-2 num text-xs">
              {typeof row.p_calibrated === "number"
                ? `Model ${percent(row.p_calibrated)}`
                : unproven
                  ? "Unproven"
                  : ""}
              {typeof row.edge === "number" ? ` · Edge ${signedPercent(row.edge)}` : ""}
            </span>
            {row.used ? (
              <StatusChip status="verified" label="On a ticket" />
            ) : (
              <span className="text-muted w-full text-xs">{reasonLabel(String(row.reason))}</span>
            )}
          </li>
        );
      })}
    </ul>
  );
}

export default async function GamePage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const game = await apiGetOptional<GameDetail>(`/games/${encodeURIComponent(id)}`);
  if (!game) notFound();
  const home = game.home_name ?? "Home";
  const away = game.away_name ?? "Away";
  const prediction = game.prediction as { ft?: Period | null; h1?: Period | null } | null;
  const full = prediction?.ft ?? null;
  const half = prediction?.h1 ?? null;
  const unprovenHandicap = game.edge_labels.handicap === "unproven";
  const unprovenTotal = game.edge_labels.total_ft === "unproven";
  const tip = String(game.game.tip_utc ?? "");
  const handicap = lineFor(game, "handicap");
  const features = game.key_features;

  return (
    <>
      <PageTitle
        title={`${away} at ${home}`}
        sub={`${game.league} · Tip ${lagosTime(tip)} Lagos`}
      />
      {game.final && (
        <p className="text-lg font-semibold">
          Final: {away} {game.final.away}, {home} {game.final.home}
        </p>
      )}

      {full ? (
        <>
          <Card>
            <p className="text-text-2 text-sm">Predicted score</p>
            <p className="mt-1 text-3xl font-semibold">
              {away} {full.mu_away.toFixed(0)} <span className="text-muted">·</span> {home}{" "}
              {full.mu_home.toFixed(0)}
            </p>
            {half && (
              <p className="text-text-2 mt-2 text-sm">
                First half: {away} {half.mu_away.toFixed(0)}, {home} {half.mu_home.toFixed(0)}
              </p>
            )}
          </Card>
          <div className="mt-4 space-y-4">
            <DistributionChart
              title={`Margin (${home} minus ${away})`}
              mean={full.mu_home - full.mu_away}
              sd={full.sd_margin}
              line={handicap === null ? null : -handicap}
              aboveLabel={`${home} covers`}
              belowLabel={`${away} covers`}
              hideChances={unprovenHandicap}
            />
            <DistributionChart
              title="Total points"
              mean={full.mu_home + full.mu_away}
              sd={full.sd_total}
              line={lineFor(game, "total_ft")}
              aboveLabel="Over"
              belowLabel="Under"
              hideChances={unprovenTotal}
            />
          </div>
        </>
      ) : (
        <Empty title="No prediction yet">This game hasn&apos;t been in a run.</Empty>
      )}

      {Object.keys(features).length > 0 && (
        <Section title="Ratings">
          <Card>
            <p className="text-text-2 mb-2 flex justify-between text-xs">
              <span className="flex items-center gap-1.5">
                <span
                  aria-hidden
                  className="inline-block h-1.5 w-3 rounded-sm"
                  style={{ background: "var(--series-1)" }}
                />
                {home}
              </span>
              <span className="flex items-center gap-1.5">
                {away}
                <span
                  aria-hidden
                  className="inline-block h-1.5 w-3 rounded-sm"
                  style={{ background: "var(--series-2)" }}
                />
              </span>
            </p>
            <ul>
              {RATINGS.map((rating) => (
                <RatingRow
                  key={rating.name}
                  name={rating.name}
                  home={features[rating.home]}
                  away={features[rating.away]}
                />
              ))}
            </ul>
          </Card>
        </Section>
      )}

      {game.league === "NBA" && (
        <Section title="Injuries">
          {game.injuries.length ? (
            <Card>
              <ul className="space-y-1 text-sm">
                {(game.injuries as Row[]).map((row, index) => (
                  <li key={index} className="flex justify-between gap-3">
                    <span>
                      {String(row.team_id) === String(game.game.home_id) ? home : away}, player{" "}
                      {String(row.player_id)}
                    </span>
                    <span className="text-text-2">{String(row.status)}</span>
                  </li>
                ))}
              </ul>
            </Card>
          ) : (
            <Empty title="Nobody listed">The latest injury report has no one for this game.</Empty>
          )}
        </Section>
      )}

      <Section title="Every outcome scored">
        <ScoredTable game={game} />
      </Section>
      <p className="mt-6 text-sm">
        <Link href={`/slate/${String(game.game.slate_date_et ?? "")}`} className="underline">
          Back to this slate
        </Link>
      </p>
    </>
  );
}
