"use client";

/**
 * The dashboard.
 *
 * A client component that fetches from the API in the browser, rather than a server-rendered
 * page. The whole app is a static export with no server of its own, so there is nowhere to
 * render on — and that is the point: the one slow dependency is a free API instance that
 * sleeps, and this way its slowness is visible as a loading state on a page that has already
 * painted, instead of a blank white screen while a server waits on it.
 *
 * Each panel loads and fails independently. A sleeping API means the whole page is slow, but
 * a broken movers query must not take the chart down with it.
 */

import { useCallback, useEffect, useState } from "react";
import { CommoditySearch } from "@/components/CommoditySearch";
import { Footer } from "@/components/Footer";
import { MoversTable } from "@/components/MoversTable";
import { PriceChart, PriceTable } from "@/components/PriceChart";
import { ChartSkeleton, Empty, Failed, TableSkeleton, Waking } from "@/components/Skeletons";
import { api, ApiError, type Commodity, type Mover, type Observation } from "@/lib/api";
import { commodityLabel, daysAgo } from "@/lib/format";

const RANGES = [
  { days: 30, label: "30 days" },
  { days: 90, label: "90 days" },
  { days: 365, label: "1 year" },
] as const;

const MOVER_WINDOWS = [
  { days: 7, label: "7 days" },
  { days: 30, label: "30 days" },
] as const;

type Load<T> = { state: "loading" } | { state: "ready"; data: T } | { state: "failed"; error: Error };

export default function Dashboard() {
  const [commodity, setCommodity] = useState<Commodity | null>(null);
  const [rangeDays, setRangeDays] = useState<number>(90);
  const [moverDays, setMoverDays] = useState<number>(7);
  const [showTable, setShowTable] = useState(false);
  const [attempt, setAttempt] = useState(0);

  const [series, setSeries] = useState<Load<Observation[]>>({ state: "loading" });
  const [movers, setMovers] = useState<Load<Mover[]>>({ state: "loading" });

  // Nothing is selected on first paint, so the page picks one rather than greeting a reader
  // with an empty frame and an invitation to think of something.
  //
  // Chosen from the movers list, not from a search for "rice". The commodity table is the
  // seeded *vocabulary*, and plenty of it has no observations yet — the top match for "rice"
  // is `Other Special Rice | White Rice`, which is seeded and has never been monitored. That
  // default rendered an empty chart on first load, which reads as "this project has no data".
  // Every mover has at least two observations by construction, so this cannot.
  useEffect(() => {
    if (commodity) return;
    const controller = new AbortController();
    api
      .movers({ window_days: 30, limit: 1 }, controller.signal)
      .then(async (page) => {
        const top = page.items[0];
        if (top) {
          setCommodity(await api.commodity(top.commodity_slug, controller.signal));
        } else {
          // Nothing to select, because nothing has been ingested. Without this the chart
          // waits on a commodity that is never coming and shows a loading skeleton for
          // ever — a database with no rows in it looked exactly like a slow request.
          setSeries({ state: "ready", data: [] });
        }
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setSeries({ state: "failed", error: error as Error });
      });
    return () => controller.abort();
  }, [commodity, attempt]);

  useEffect(() => {
    if (!commodity) return;
    const controller = new AbortController();
    setSeries({ state: "loading" });
    api
      .observations(
        { commodity: commodity.canonical_slug, date_from: daysAgo(rangeDays), limit: 500 },
        controller.signal,
      )
      .then((page) => setSeries({ state: "ready", data: page.items }))
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setSeries({ state: "failed", error: error as Error });
      });
    return () => controller.abort();
  }, [commodity, rangeDays, attempt]);

  useEffect(() => {
    const controller = new AbortController();
    setMovers({ state: "loading" });
    api
      .movers({ window_days: moverDays, limit: 12 }, controller.signal)
      .then((page) => setMovers({ state: "ready", data: page.items }))
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setMovers({ state: "failed", error: error as Error });
      });
    return () => controller.abort();
  }, [moverDays, attempt]);

  const retry = useCallback(() => setAttempt((count) => count + 1), []);

  // "Waking up" is shown only while nothing at all has arrived. Once one panel has answered
  // the API is demonstrably awake, and repeating the excuse would be noise.
  const cold = series.state === "loading" && movers.state === "loading";

  return (
    <main className="shell">
      <header className="masthead">
        <div>
          <h1>PresyoWatch</h1>
          <p>
            Daily retail prices for agricultural and fishery commodities in the Philippines,
            taken from the Department of Agriculture&rsquo;s Bantay Presyo monitoring sheets.
          </p>
        </div>
      </header>

      {cold ? <Waking /> : null}

      <section className="card" aria-labelledby="series-heading">
        <header>
          <h2 id="series-heading">
            {commodity
              ? commodityLabel(commodity.name, commodity.specification)
              : "Price over time"}
          </h2>
          <div className="segmented" role="group" aria-label="Time range">
            {RANGES.map((range) => (
              <button
                key={range.days}
                type="button"
                aria-pressed={rangeDays === range.days}
                onClick={() => setRangeDays(range.days)}
              >
                {range.label}
              </button>
            ))}
            <button
              type="button"
              aria-pressed={showTable}
              onClick={() => setShowTable((shown) => !shown)}
            >
              {showTable ? "Chart" : "Table"}
            </button>
          </div>
        </header>

        <div className="controls">
          <CommoditySearch onSelect={setCommodity} selected={commodity} />
        </div>

        {commodity ? (
          <p className="note">
            {commodity.group} · priced per {commodity.unit}
            {commodity.is_agricultural_input
              ? " · a farm input, not a food item"
              : ""}
          </p>
        ) : null}

        {series.state === "loading" ? (
          <ChartSkeleton />
        ) : series.state === "failed" ? (
          <Failed error={series.error as ApiError} onRetry={retry} />
        ) : series.data.length === 0 ? (
          <Empty>
            {commodity
              ? `No monitoring was published for this commodity in the last ${rangeDays} days.
                 Try a longer range, or another commodity — coverage varies a lot by market.`
              : "The API is running but has no observations in it yet, so there is nothing to chart. That means no ingestion run has completed successfully."}
          </Empty>
        ) : showTable ? (
          <PriceTable observations={series.data} />
        ) : (
          <PriceChart observations={series.data} unit={commodity?.unit ?? "kg"} />
        )}
      </section>

      <section className="card" aria-labelledby="movers-heading">
        <header>
          <h2 id="movers-heading">Biggest movers</h2>
          <div className="segmented" role="group" aria-label="Comparison window">
            {MOVER_WINDOWS.map((window) => (
              <button
                key={window.days}
                type="button"
                aria-pressed={moverDays === window.days}
                onClick={() => setMoverDays(window.days)}
              >
                {window.label}
              </button>
            ))}
          </div>
        </header>
        <p className="note">
          Compared at a single market, never averaged across markets. The two dates are the
          first and last days actually monitored inside the window — the source does not
          publish every day. A rise is shown in red because for a shopper that is the bad
          direction.
        </p>

        {movers.state === "loading" ? (
          <TableSkeleton />
        ) : movers.state === "failed" ? (
          <Failed error={movers.error as ApiError} onRetry={retry} />
        ) : movers.data.length === 0 ? (
          <Empty>
            Nothing has two days of monitoring inside this window yet. A longer window, or a
            few more ingestion runs, will fill it.
          </Empty>
        ) : (
          <MoversTable movers={movers.data} />
        )}
      </section>

      <Footer />
    </main>
  );
}
