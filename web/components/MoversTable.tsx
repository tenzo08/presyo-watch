"use client";

/**
 * The biggest movers table.
 *
 * Every row names its market, because that is what the API compared. Averaging Butuan and
 * Tandag into "the price of tomatoes" would produce a number that is nobody's price and
 * that moves when coverage changes rather than when prices do.
 *
 * The two dates are shown rather than hidden behind "last 7 days". The source does not
 * publish daily, so the real comparison is between the first and last figures *inside* the
 * window, and a reader deserves to see that a "7-day change" was measured over four.
 *
 * Direction is never colour alone: an arrow and a signed number carry it, and the colour is
 * a reinforcement. Red is a price rise, which for a shopper is the bad direction — the
 * opposite of a stock chart, and worth being explicit about.
 */

import type { Mover } from "@/lib/api";
import { longDate, percent, peso } from "@/lib/format";

export function MoversTable({ movers }: { movers: Mover[] }) {
  return (
    <div className="scroller">
      <table>
        <thead>
          <tr>
            <th scope="col">Commodity</th>
            <th scope="col">Market</th>
            <th scope="col">Compared</th>
            <th scope="col" className="amount">From</th>
            <th scope="col" className="amount">To</th>
            <th scope="col" className="amount">Change</th>
          </tr>
        </thead>
        <tbody>
          {movers.map((mover) => {
            const rose = mover.percent_change > 0;
            const flat = mover.percent_change === 0;
            return (
              <tr key={`${mover.commodity_slug}-${mover.market_id}`}>
                <th scope="row" style={{ fontWeight: 500, textTransform: "none", fontSize: 14 }}>
                  {mover.commodity}
                  <span className="group" style={{ display: "block", fontSize: 12 }}>
                    per {mover.unit}
                  </span>
                </th>
                <td>
                  {mover.market}
                  <span className="group" style={{ display: "block", fontSize: 12 }}>
                    {mover.municipality}
                  </span>
                </td>
                <td style={{ fontSize: 13 }}>
                  {longDate(mover.first_observed_on)} → {longDate(mover.last_observed_on)}
                  <span className="group" style={{ display: "block", fontSize: 12 }}>
                    {mover.observations} days monitored
                  </span>
                </td>
                <td className="amount">{peso(mover.first_average)}</td>
                <td className="amount">{peso(mover.last_average)}</td>
                <td className={`amount ${flat ? "" : rose ? "rose" : "fell"}`}>
                  <span aria-hidden="true">{flat ? "→" : rose ? "↑" : "↓"}</span>{" "}
                  {percent(mover.percent_change)}
                  <span className="group" style={{ display: "block", fontSize: 12 }}>
                    {peso(mover.change)}
                  </span>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
