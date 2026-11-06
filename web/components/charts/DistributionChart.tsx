"use client";

import {
  Area,
  AreaChart,
  CartesianGrid,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { normalBelow, normalDensity, percent } from "@/lib/format";

import { AXIS_TICK, ChartFrame, TOOLTIP_STYLE } from "./ChartFrame";

const POINTS = 81;
const SPREAD = 3.2;

/**
 * The forecast as a curve, split at the line: amber above it, blue below. An integer line can push,
 * so the chances keep a push share.
 */
export function DistributionChart({
  title,
  mean,
  sd,
  line,
  aboveLabel,
  belowLabel,
  hideChances = false,
}: {
  title: string;
  mean: number;
  sd: number;
  line: number | null;
  aboveLabel: string;
  belowLabel: string;
  hideChances?: boolean;
}) {
  const data = Array.from({ length: POINTS }, (_, index) => {
    const x = mean - SPREAD * sd + (index * 2 * SPREAD * sd) / (POINTS - 1);
    const y = normalDensity(x, mean, sd);
    return {
      x: Math.round(x * 10) / 10,
      above: line === null || x >= line ? y : null,
      below: line !== null && x <= line ? y : null,
    };
  });
  const whole = line !== null && Number.isInteger(line);
  const belowChance = line === null ? null : normalBelow(whole ? line - 0.5 : line, mean, sd);
  const aboveChance = line === null ? null : 1 - normalBelow(whole ? line + 0.5 : line, mean, sd);
  const pushChance =
    belowChance === null || aboveChance === null ? 0 : Math.max(0, 1 - belowChance - aboveChance);
  const showChances = !hideChances && line !== null;

  return (
    <ChartFrame
      title={title}
      summary={
        line === null
          ? `Expected ${mean.toFixed(1)}, give or take ${sd.toFixed(1)}. No line was offered.`
          : `Expected ${mean.toFixed(1)}, give or take ${sd.toFixed(1)}, against a line of ${line}.`
      }
      legend={
        line === null
          ? undefined
          : [
              { label: aboveLabel, color: "var(--series-1)" },
              { label: belowLabel, color: "var(--series-2)" },
            ]
      }
      columns={["Measure", "Value"]}
      rows={[
        ["Expected", mean.toFixed(1)],
        ["Spread", sd.toFixed(1)],
        ...(line === null ? [] : [["Line", line] as const]),
        ...(showChances
          ? ([
              [aboveLabel, percent(aboveChance)],
              [belowLabel, percent(belowChance)],
              ...(whole ? [["Push", percent(pushChance)] as const] : []),
            ] as const)
          : []),
      ]}
    >
      <ResponsiveContainer width="100%" height={190}>
        <AreaChart data={data} margin={{ top: 16, right: 8, bottom: 0, left: 8 }}>
          <CartesianGrid vertical={false} stroke="var(--grid)" />
          <XAxis
            dataKey="x"
            type="number"
            domain={["dataMin", "dataMax"]}
            tick={AXIS_TICK}
            tickLine={false}
            axisLine={{ stroke: "var(--axis)" }}
            tickFormatter={(value: number) => String(Math.round(value))}
          />
          <YAxis hide />
          <Tooltip
            contentStyle={TOOLTIP_STYLE}
            labelFormatter={(value) => `Score ${value}`}
            formatter={(value) => [`${(Number(value) * 100).toFixed(1)}% per point`, "Density"]}
          />
          <Area
            dataKey="above"
            type="monotone"
            stroke="var(--series-1)"
            strokeWidth={2}
            fill="var(--series-1)"
            fillOpacity={0.12}
            connectNulls={false}
            isAnimationActive={false}
          />
          <Area
            dataKey="below"
            type="monotone"
            stroke="var(--series-2)"
            strokeWidth={2}
            fill="var(--series-2)"
            fillOpacity={0.12}
            connectNulls={false}
            isAnimationActive={false}
          />
          {line !== null && (
            <ReferenceLine
              x={line}
              stroke="var(--text-2)"
              label={{
                value: `Line ${line}`,
                position: "top",
                fill: "var(--text-2)",
                fontSize: 12,
              }}
            />
          )}
        </AreaChart>
      </ResponsiveContainer>
    </ChartFrame>
  );
}
