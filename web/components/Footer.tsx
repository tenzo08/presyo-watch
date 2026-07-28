"use client";

/**
 * Source attribution.
 *
 * Not a courtesy and not boilerplate: attribution is a condition of using this data
 * (CLAUDE.md rule 8), and the text is fetched from the API's own `/meta/sources` rather than
 * hard-coded, so it can never drift from what the ingester actually recorded. A hard-coded
 * fallback is shown if the API is asleep, because a page that renders without attribution is
 * worse than a page that renders slowly.
 */

import { useEffect, useState } from "react";
import { api, type Source } from "@/lib/api";

export function Footer() {
  const [sources, setSources] = useState<Source[] | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    api
      .sources(controller.signal)
      .then(setSources)
      .catch(() => setSources(null));
    return () => controller.abort();
  }, []);

  return (
    <footer className="attribution">
      <h2>Data sources</h2>
      {sources?.length ? (
        sources.map((source) => (
          <p key={source.slug}>
            {source.attribution_text}{" "}
            <a href={source.base_url} rel="noreferrer noopener">
              {source.base_url}
            </a>
            {source.licence ? ` — ${source.licence}` : null}
          </p>
        ))
      ) : (
        <p>
          Bantay Presyo price monitoring published by the Department of Agriculture Regional
          Field Office XIII (Caraga). Reproduced under RA 8293 § 176.
        </p>
      )}
      <p>
        Philippine Statistics Authority data, where used, is licensed{" "}
        <a
          href="https://creativecommons.org/licenses/by/4.0/"
          rel="noreferrer noopener license"
        >
          CC BY 4.0
        </a>
        .
      </p>
      <p>
        Prices are shown as published by the source agency. This project does not correct,
        smooth or interpolate them, and gaps are shown as gaps.
      </p>
    </footer>
  );
}
