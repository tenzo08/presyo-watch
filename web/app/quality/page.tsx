"use client";

/**
 * The public data quality page.
 *
 * PLANNING.md: "Observability is a feature, not an afterthought. The data quality page is
 * public and shows ingestion success rate, null rate per commodity, quarantine count, and
 * last-successful-run per source. This is the single highest-signal thing to a reviewer."
 *
 * So it is deliberately unflattering. It leads with what did *not* load. A source that has
 * never run says so in the first row rather than being absent from the table, because
 * "nothing has ever run" and "there is no such source" look identical when a row is missing
 * and mean completely different things.
 */

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { Footer } from "@/components/Footer";
import { Failed, TableSkeleton } from "@/components/Skeletons";
import { api, ApiError, type Quality } from "@/lib/api";
import { longDate, percent } from "@/lib/format";

type Load =
  | { state: "loading" }
  | { state: "ready"; data: Quality }
  | { state: "failed"; error: Error };

function when(timestamp: string | null): string {
  if (!timestamp) return "never";
  return new Date(timestamp).toLocaleString("en-GB", {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

export default function QualityPage() {
  const [load, setLoad] = useState<Load>({ state: "loading" });
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setLoad({ state: "loading" });
    api
      .quality(controller.signal)
      .then((data) => setLoad({ state: "ready", data }))
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setLoad({ state: "failed", error: error as Error });
      });
    return () => controller.abort();
  }, [attempt]);

  const retry = useCallback(() => setAttempt((count) => count + 1), []);
  const quarantined =
    load.state === "ready"
      ? load.data.quarantine.reduce((total, stage) => total + stage.rows, 0)
      : 0;

  return (
    <main className="shell">
      <header className="masthead">
        <div>
          <h1>Data quality</h1>
          <p>
            What loaded, and what did not. Published because a project that only shows its
            successes is not evidence of anything.
          </p>
        </div>
        <Link href="/">← Prices</Link>
      </header>

      {load.state === "loading" ? (
        <div className="card">
          <TableSkeleton rows={5} />
        </div>
      ) : load.state === "failed" ? (
        <div className="card">
          <Failed error={load.error as ApiError} onRetry={retry} />
        </div>
      ) : (
        <>
          <section className="card" aria-labelledby="sources-heading">
            <header>
              <h2 id="sources-heading">Ingestion, by source</h2>
            </header>
            <p className="note">
              A run is recorded even when it fails, so a broken or missed run is visible here
              rather than absent. <strong>Partial</strong> means some files landed and some
              were quarantined — the normal state of a source that publishes dead links.
            </p>
            <div className="scroller">
              <table>
                <thead>
                  <tr>
                    <th scope="col">Source</th>
                    <th scope="col" className="amount">Runs</th>
                    <th scope="col" className="amount">Succeeded</th>
                    <th scope="col" className="amount">Partial</th>
                    <th scope="col" className="amount">Failed</th>
                    <th scope="col">Last run</th>
                    <th scope="col">Last success</th>
                  </tr>
                </thead>
                <tbody>
                  {load.data.sources.map((source) => (
                    <tr key={source.slug}>
                      <th scope="row" style={{ fontWeight: 500, textTransform: "none", fontSize: 14 }}>
                        {source.name}
                      </th>
                      <td className="amount">{source.runs}</td>
                      <td className="amount">
                        {source.succeeded}
                        {source.runs > 0 ? (
                          <span className="group" style={{ display: "block", fontSize: 12 }}>
                            {percent((source.succeeded / source.runs) * 100).replace("+", "")}
                          </span>
                        ) : null}
                      </td>
                      <td className="amount">{source.partial}</td>
                      <td className={`amount ${source.failed > 0 ? "rose" : ""}`}>
                        {source.failed}
                      </td>
                      <td style={{ fontSize: 13 }}>{when(source.last_run_at)}</td>
                      <td
                        style={{ fontSize: 13 }}
                        className={source.last_success_at ? "" : "rose"}
                      >
                        {when(source.last_success_at)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {load.data.sources.length === 0 ? (
              <p className="state">
                <strong>No sources are seeded.</strong>
                The reference data has never been loaded into this database.
              </p>
            ) : null}
          </section>

          <section className="card" aria-labelledby="quarantine-heading">
            <header>
              <h2 id="quarantine-heading">Quarantine</h2>
            </header>
            <p className="note">
              Rows that could not be stored, kept with the reason and enough of the original
              to be reprocessed after a fix. Nothing is ever dropped silently — this count is
              the honest denominator for everything on the prices page.
            </p>
            {quarantined === 0 ? (
              <p className="state">Nothing is quarantined.</p>
            ) : (
              <div className="scroller">
                <table>
                  <thead>
                    <tr>
                      <th scope="col">Stage</th>
                      <th scope="col" className="amount">Rows</th>
                      <th scope="col">An example reason</th>
                    </tr>
                  </thead>
                  <tbody>
                    {load.data.quarantine.map((stage) => (
                      <tr key={stage.stage}>
                        <th scope="row" style={{ fontWeight: 500, textTransform: "none", fontSize: 14 }}>
                          {stage.stage}
                        </th>
                        <td className="amount">{stage.rows.toLocaleString()}</td>
                        <td style={{ whiteSpace: "normal", fontSize: 13 }}>
                          {stage.example_reason ?? "—"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          <section className="card" aria-labelledby="coverage-heading">
            <header>
              <h2 id="coverage-heading">Coverage</h2>
            </header>
            <p className="note">
              <strong>Unavailable</strong> rows are commodities the source listed and
              published no figures for. They are expected — 40 to 75 of about 150 rows on a
              typical sheet — and are not a fault. <strong>Impossible</strong> rows
              contradict themselves arithmetically, with an average outside their own
              low-to-high range; those are the source&rsquo;s errors, kept as published.
            </p>
            <div className="scroller">
              <table>
                <tbody>
                  <tr>
                    <th scope="row" style={{ fontWeight: 500, textTransform: "none", fontSize: 14 }}>
                      Observations stored
                    </th>
                    <td className="amount">{load.data.observations.toLocaleString()}</td>
                  </tr>
                  <tr>
                    <th scope="row" style={{ fontWeight: 500, textTransform: "none", fontSize: 14 }}>
                      Listed but not monitored
                    </th>
                    <td className="amount">{load.data.unavailable.toLocaleString()}</td>
                  </tr>
                  <tr>
                    <th scope="row" style={{ fontWeight: 500, textTransform: "none", fontSize: 14 }}>
                      Arithmetically impossible
                    </th>
                    <td className={`amount ${load.data.impossible > 0 ? "rose" : ""}`}>
                      {load.data.impossible.toLocaleString()}
                    </td>
                  </tr>
                  <tr>
                    <th scope="row" style={{ fontWeight: 500, textTransform: "none", fontSize: 14 }}>
                      Commodities ever observed
                    </th>
                    <td className="amount">
                      {load.data.commodities_observed.toLocaleString()} of{" "}
                      {load.data.commodities_seeded.toLocaleString()}
                    </td>
                  </tr>
                  <tr>
                    <th scope="row" style={{ fontWeight: 500, textTransform: "none", fontSize: 14 }}>
                      Dates covered
                    </th>
                    <td className="amount">
                      {load.data.earliest_observed_on
                        ? `${longDate(load.data.earliest_observed_on)} — ${longDate(
                            load.data.latest_observed_on ?? load.data.earliest_observed_on,
                          )}`
                        : "—"}
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </section>
        </>
      )}

      <Footer />
    </main>
  );
}
