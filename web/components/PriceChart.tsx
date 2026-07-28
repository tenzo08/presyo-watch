"use client";

/**
 * One commodity's price over time, one line per market.
 *
 * **The form.** The job is change-over-time with identity — "did rice get dearer, and where"
 * — which is a multi-series line. Markets are the categorical dimension, so each gets a
 * fixed slot from the validated palette in a fixed order. The slot follows the *market*, not
 * its rank, so deselecting one never repaints the others.
 *
 * **Gaps stay gaps.** The source does not publish every day, and this chart does not pretend
 * otherwise: a missing day is `null` and `connectNulls` is off, so the line breaks. Joining
 * across a gap would draw a week of prices nobody measured, which is exactly the
 * interpolation PLANNING.md forbids at rest — doing it in pixels instead of in the database
 * would be the same lie with better manners.
 *
 * **No zero baseline.** Prices are compared against each other, not against nothing; forcing
 * a ₱0 origin would flatten every real movement into a straight line near the top. The axis
 * is labelled and the domain is stated, so nobody reads a 5% move as a collapse.
 *
 * **Never a second y-axis.** If two commodities with different price scales are ever
 * compared here, they get two charts, not two axes.
 *
 * Six markets is the cap. The palette's seventh and eighth slots exist, but past six the
 * lines stop being separable at a glance whatever their colour.
 */

import { useMemo } from "react";
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { Observation } from "@/lib/api";
import { peso, shortDate, longDate } from "@/lib/format";

export const MAX_MARKETS = 6;

/** Fixed slots. Assigned in order and never cycled — see `globals.css` for why. */
const SERIES = [
  "var(--series-1)",
  "var(--series-2)",
  "var(--series-3)",
  "var(--series-4)",
  "var(--series-5)",
  "var(--series-6)",
] as const;

export interface Series {
  marketId: number;
  label: string;
  colour: string;
}

interface Row {
  observed_on: string;
  [marketKey: string]: string | number | null;
}

/** Group observations into one row per date, one column per market. */
export function toSeries(observations: Observation[]): { rows: Row[]; series: Series[] } {
  const markets = new Map<number, string>();
  for (const observation of observations) {
    if (!markets.has(observation.market_id)) {
      markets.set(observation.market_id, `${observation.market}, ${observation.municipality}`);
    }
  }
  // Sorted by id, so a market keeps its colour between renders and between page loads
  // rather than depending on which day happened to be fetched first.
  const chosen = [...markets.entries()].sort((a, b) => a[0] - b[0]).slice(0, MAX_MARKETS);
  const series: Series[] = chosen.map(([marketId, label], index) => ({
    marketId,
    label,
    colour: SERIES[index % SERIES.length] as string,
  }));
  const included = new Set(series.map((entry) => entry.marketId));

  const byDate = new Map<string, Row>();
  for (const observation of observations) {
    if (!included.has(observation.market_id) || observation.average === null) continue;
    let row = byDate.get(observation.observed_on);
    if (!row) {
      row = { observed_on: observation.observed_on };
      // Every market gets an explicit null on every date. Without it Recharts has no point
      // to break the line at, and a gap would silently close up.
      for (const entry of series) row[`m${entry.marketId}`] = null;
      byDate.set(observation.observed_on, row);
    }
    row[`m${observation.market_id}`] = Number(observation.average);
  }

  return {
    rows: [...byDate.values()].sort((a, b) => a.observed_on.localeCompare(b.observed_on)),
    series,
  };
}

interface TooltipEntry {
  dataKey?: string | number;
  value?: number | string | null;
}

function ChartTooltip({
  active,
  payload,
  label,
  series,
}: {
  active?: boolean;
  payload?: TooltipEntry[];
  label?: string;
  series: Series[];
}) {
  if (!active || !payload?.length || !label) return null;
  const shown = payload.filter((entry) => entry.value !== null && entry.value !== undefined);
  if (!shown.length) return null;

  return (
    <div className="tooltip">
      <div className="when">{longDate(label)}</div>
      <ul>
        {shown.map((entry) => {
          const found = series.find((item) => `m${item.marketId}` === entry.dataKey);
          if (!found) return null;
          return (
            <li key={found.marketId}>
              <span className="swatch" style={{ background: found.colour }} />
              <span>{found.label}</span>
              <span className="amount">{peso(String(entry.value))}</span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

export function PriceChart({
  observations,
  unit,
}: {
  observations: Observation[];
  unit: string;
}) {
  const { rows, series } = useMemo(() => toSeries(observations), [observations]);

  if (!rows.length) return null;

  return (
    <figure style={{ margin: 0 }}>
      {/* A legend is always present for two or more series, so identity never rests on
          colour alone. With six or fewer it is also the direct-label channel the light
          palette's contrast warning requires. */}
      <ul className="legend">
        {series.map((entry) => (
          <li key={entry.marketId}>
            <span className="swatch" style={{ background: entry.colour }} />
            {entry.label}
          </li>
        ))}
      </ul>

      <ResponsiveContainer width="100%" height={300}>
        <LineChart data={rows} margin={{ top: 8, right: 16, bottom: 4, left: 4 }}>
          {/* Recessive chrome: horizontal rules only, hairline weight. Vertical gridlines
              would compete with the lines they are meant to sit behind. */}
          <CartesianGrid stroke="var(--gridline)" strokeWidth={1} vertical={false} />
          <XAxis
            dataKey="observed_on"
            tickFormatter={shortDate}
            tick={{ fill: "var(--text-muted)", fontSize: 12 }}
            stroke="var(--axis)"
            tickLine={false}
            minTickGap={28}
          />
          <YAxis
            // Not zero-based, deliberately. See the module docstring.
            domain={["auto", "auto"]}
            tick={{ fill: "var(--text-muted)", fontSize: 12 }}
            stroke="var(--axis)"
            tickLine={false}
            axisLine={false}
            width={64}
            tickFormatter={(value: number) => `₱${value.toFixed(0)}`}
            label={{
              value: `₱ per ${unit}`,
              angle: -90,
              position: "insideLeft",
              style: { fill: "var(--text-muted)", fontSize: 12, textAnchor: "middle" },
            }}
          />
          <Tooltip
            content={<ChartTooltip series={series} />}
            cursor={{ stroke: "var(--axis)", strokeWidth: 1 }}
          />
          {series.map((entry) => (
            <Line
              key={entry.marketId}
              type="linear"
              dataKey={`m${entry.marketId}`}
              name={entry.label}
              stroke={entry.colour}
              strokeWidth={2}
              // A gap in monitoring is a gap in the line.
              connectNulls={false}
              // 8px dots with a 2px surface ring, so two markets at the same price on the
              // same day stay two readable marks rather than one muddy blob.
              dot={{ r: 4, strokeWidth: 2, stroke: "var(--surface-1)", fill: entry.colour }}
              activeDot={{ r: 5, strokeWidth: 2, stroke: "var(--surface-1)" }}
              isAnimationActive={false}
            />
          ))}
        </LineChart>
      </ResponsiveContainer>
      <figcaption className="note" style={{ marginTop: 10 }}>
        Prevailing average price per {unit}. A break in a line is a day the market was not
        monitored, not a day the price was zero.
      </figcaption>
    </figure>
  );
}

/** The table view. Required relief for the light palette's sub-3:1 steps, and the honest
 *  answer for anyone who would rather read numbers than a picture. */
export function PriceTable({ observations }: { observations: Observation[] }) {
  const { rows, series } = useMemo(() => toSeries(observations), [observations]);

  return (
    <div className="scroller">
      <table>
        <caption className="note" style={{ textAlign: "left", captionSide: "top" }}>
          The same figures as the chart. An em dash is a day with no monitoring.
        </caption>
        <thead>
          <tr>
            <th scope="col">Date</th>
            {series.map((entry) => (
              <th key={entry.marketId} scope="col" className="amount">
                <span className="swatch" style={{ background: entry.colour, marginRight: 6 }} />
                {entry.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.observed_on}>
              <th scope="row" style={{ fontWeight: 400, textTransform: "none", fontSize: 14 }}>
                {longDate(row.observed_on)}
              </th>
              {series.map((entry) => {
                const value = row[`m${entry.marketId}`];
                return (
                  <td key={entry.marketId} className="amount">
                    {typeof value === "number" ? peso(value.toFixed(2)) : "—"}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
