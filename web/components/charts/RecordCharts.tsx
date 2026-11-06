"use client";

import {
  Bar,
  BarChart,
  CartesianGrid,
  ComposedChart,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { percent, rollingHitRate } from "@/lib/format";
import type { TrackRecord } from "@/lib/types";

import { AXIS_TICK, ChartFrame, TOOLTIP_STYLE } from "./ChartFrame";

const asPercent = (value: number) => `${Math.round(value * 100)}%`;

export function ReliabilityChart({ buckets }: { buckets: TrackRecord["buckets"] }) {
  const points = buckets.filter((item) => item.legs > 0 && item.mean_probability !== null);
  const low = Math.min(0.4, ...points.map((item) => item.mean_probability ?? 1));
  const high = Math.max(0.8, ...points.map((item) => item.mean_probability ?? 0));
  const data = points.map((item) => ({
    predicted: item.mean_probability,
    actual: item.hit_rate,
    legs: item.legs,
  }));
  const worst = points.reduce(
    (largest, item) =>
      Math.max(largest, Math.abs((item.mean_probability ?? 0) - (item.hit_rate ?? 0))),
    0,
  );
  return (
    <ChartFrame
      title="Did the probabilities come true?"
      summary={`A perfect model sits on the diagonal. The widest gap in any bucket is ${percent(worst)}.`}
      legend={[
        { label: "What happened", color: "var(--series-1)" },
        { label: "Perfectly calibrated", color: "var(--axis)" },
      ]}
      columns={["Predicted", "Actual", "Legs"]}
      rows={points.map((item) => [
        percent(item.mean_probability),
        percent(item.hit_rate),
        item.legs,
      ])}
    >
      <ResponsiveContainer width="100%" height={240}>
        <ComposedChart data={data} margin={{ top: 8, right: 12, bottom: 0, left: 0 }}>
          <CartesianGrid stroke="var(--grid)" />
          <XAxis
            dataKey="predicted"
            type="number"
            domain={[low, high]}
            tickFormatter={asPercent}
            tick={AXIS_TICK}
            tickLine={false}
            axisLine={{ stroke: "var(--axis)" }}
          />
          <YAxis
            type="number"
            domain={[low, high]}
            tickFormatter={asPercent}
            tick={AXIS_TICK}
            tickLine={false}
            axisLine={false}
            width={40}
          />
          <Tooltip
            contentStyle={TOOLTIP_STYLE}
            formatter={(value) => percent(Number(value))}
            labelFormatter={(value) => `Predicted ${percent(Number(value))}`}
          />
          <ReferenceLine
            segment={[
              { x: low, y: low },
              { x: high, y: high },
            ]}
            stroke="var(--axis)"
          />
          <Line
            dataKey="actual"
            name="What happened"
            stroke="var(--series-1)"
            strokeWidth={2}
            dot={{ r: 4, fill: "var(--series-1)", stroke: "var(--surface)", strokeWidth: 2 }}
            isAnimationActive={false}
          />
        </ComposedChart>
      </ResponsiveContainer>
    </ChartFrame>
  );
}

export function BucketChart({ buckets }: { buckets: TrackRecord["buckets"] }) {
  const data = buckets
    .filter((item) => item.legs > 0)
    .map((item) => ({
      name: `${Math.round(item.bucket * 100)}%`,
      rate: item.hit_rate ?? 0,
      legs: item.legs,
    }));
  const largest = data.reduce(
    (best, item) => (item.legs > (best?.legs ?? 0) ? item : best),
    data[0],
  );
  return (
    <ChartFrame
      title="Hit rate by probability bucket"
      summary={
        largest
          ? `Most legs fall in the ${largest.name} bucket, where ${percent(largest.rate)} won.`
          : "No settled legs yet."
      }
      columns={["Bucket", "Hit rate", "Legs"]}
      rows={data.map((item) => [item.name, percent(item.rate), item.legs])}
    >
      <ResponsiveContainer width="100%" height={200}>
        <BarChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
          <CartesianGrid vertical={false} stroke="var(--grid)" />
          <XAxis
            dataKey="name"
            tick={AXIS_TICK}
            tickLine={false}
            axisLine={{ stroke: "var(--axis)" }}
          />
          <YAxis
            domain={[0, 1]}
            tickFormatter={asPercent}
            tick={AXIS_TICK}
            tickLine={false}
            axisLine={false}
            width={40}
          />
          <Tooltip
            contentStyle={TOOLTIP_STYLE}
            cursor={{ fill: "var(--surface-2)" }}
            formatter={(value) => [percent(Number(value)), "Hit rate"]}
          />
          <Bar
            dataKey="rate"
            fill="var(--series-1)"
            maxBarSize={24}
            radius={[4, 4, 0, 0]}
            isAnimationActive={false}
          />
        </BarChart>
      </ResponsiveContainer>
    </ChartFrame>
  );
}

export function RollingChart({
  daily,
  breakeven,
}: {
  daily: TrackRecord["daily"];
  breakeven: number | null | undefined;
}) {
  const data = rollingHitRate(daily);
  const last = data[data.length - 1];
  return (
    <ChartFrame
      title="Rolling 14-day hit rate"
      summary={
        last
          ? `Latest: ${percent(last.rate)} on ${last.settled} settled legs` +
            (breakeven ? `, against ${percent(breakeven)} needed to break even.` : ".")
          : "No settled legs yet."
      }
      columns={["Date", "Hit rate", "Settled legs"]}
      rows={data.map((item) => [item.date, percent(item.rate), item.settled])}
    >
      <ResponsiveContainer width="100%" height={200}>
        <LineChart data={data} margin={{ top: 16, right: 12, bottom: 0, left: 0 }}>
          <CartesianGrid vertical={false} stroke="var(--grid)" />
          <XAxis
            dataKey="date"
            tick={AXIS_TICK}
            tickLine={false}
            axisLine={{ stroke: "var(--axis)" }}
            tickFormatter={(value: string) => value.slice(5)}
            minTickGap={24}
          />
          <YAxis
            domain={[0, 1]}
            tickFormatter={asPercent}
            tick={AXIS_TICK}
            tickLine={false}
            axisLine={false}
            width={40}
          />
          <Tooltip
            contentStyle={TOOLTIP_STYLE}
            formatter={(value) => [percent(Number(value)), "Hit rate"]}
          />
          {breakeven && (
            <ReferenceLine
              y={breakeven}
              stroke="var(--text-2)"
              label={{
                value: "Break-even",
                position: "insideTopRight",
                fill: "var(--text-2)",
                fontSize: 12,
              }}
            />
          )}
          <Line
            dataKey="rate"
            stroke="var(--series-1)"
            strokeWidth={2}
            dot={false}
            isAnimationActive={false}
          />
        </LineChart>
      </ResponsiveContainer>
    </ChartFrame>
  );
}

export function LearningCurve({
  points,
}: {
  points: readonly { at: string; version: string; gain: number; decision: string }[];
}) {
  const last = points[points.length - 1];
  return (
    <ChartFrame
      title="Challenger against champion"
      summary={
        last
          ? `Above zero means the challenger scored better. Latest: ${last.gain.toFixed(5)}, ${last.decision}.`
          : "No comparison yet."
      }
      columns={["Date", "Log-likelihood gain", "Decision"]}
      rows={points.map((item) => [item.at.slice(0, 10), item.gain.toFixed(5), item.decision])}
    >
      <ResponsiveContainer width="100%" height={200}>
        <LineChart data={[...points]} margin={{ top: 8, right: 12, bottom: 0, left: 8 }}>
          <CartesianGrid vertical={false} stroke="var(--grid)" />
          <XAxis
            dataKey="at"
            tick={AXIS_TICK}
            tickLine={false}
            axisLine={{ stroke: "var(--axis)" }}
            tickFormatter={(value: string) => value.slice(5, 10)}
            minTickGap={24}
          />
          <YAxis
            tick={AXIS_TICK}
            tickLine={false}
            axisLine={false}
            width={64}
            tickCount={3}
            tickFormatter={(value: number) => value.toFixed(4)}
          />
          <Tooltip contentStyle={TOOLTIP_STYLE} />
          <ReferenceLine y={0} stroke="var(--axis)" />
          <Line
            dataKey="gain"
            stroke="var(--series-1)"
            strokeWidth={2}
            dot={{ r: 4, fill: "var(--series-1)", stroke: "var(--surface)", strokeWidth: 2 }}
            isAnimationActive={false}
          />
        </LineChart>
      </ResponsiveContainer>
    </ChartFrame>
  );
}
