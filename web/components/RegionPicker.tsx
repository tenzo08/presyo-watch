"use client";

/**
 * Narrowing the view to one province.
 *
 * **Only provinces that actually have a monitored market are offered.** The seed also holds
 * the region row itself — Caraga, PSGC 160000000 — and markets hang off provinces, so
 * choosing the region would filter to nothing and read as "no data here" rather than "that
 * is not a filter". A dropdown entry that can only ever return an empty result is a trap,
 * so the options are derived from the markets that exist rather than from the region table.
 *
 * The market count travels with each option for the same reason: a province with one market
 * and a province with three produce very different-looking charts, and saying so up front is
 * cheaper than letting a reader work it out from a legend.
 */

import { useEffect, useState } from "react";
import { api, type Market, type Region } from "@/lib/api";

export interface RegionOption {
  psgc_code: string;
  name: string;
  markets: number;
}

export function useRegionOptions(): RegionOption[] {
  const [options, setOptions] = useState<RegionOption[]>([]);

  useEffect(() => {
    const controller = new AbortController();
    Promise.all([api.regions(controller.signal), api.markets({ limit: 500 }, controller.signal)])
      .then(([regions, markets]) => setOptions(build(regions, markets.items)))
      .catch(() => setOptions([]));
    return () => controller.abort();
  }, []);

  return options;
}

function build(regions: Region[], markets: Market[]): RegionOption[] {
  const counts = new Map<string, number>();
  for (const market of markets) {
    counts.set(market.region_psgc_code, (counts.get(market.region_psgc_code) ?? 0) + 1);
  }
  return regions
    .filter((region) => counts.has(region.psgc_code))
    .map((region) => ({
      psgc_code: region.psgc_code,
      name: region.name,
      markets: counts.get(region.psgc_code) ?? 0,
    }))
    .sort((a, b) => a.name.localeCompare(b.name));
}

export function RegionPicker({
  options,
  value,
  onChange,
}: {
  options: RegionOption[];
  value: string | null;
  onChange: (psgc: string | null) => void;
}) {
  return (
    <label style={{ display: "flex", alignItems: "center", gap: 8 }}>
      <span className="group" style={{ fontSize: 13 }}>
        Province
      </span>
      <select
        value={value ?? ""}
        onChange={(event) => onChange(event.target.value || null)}
        aria-label="Filter by province"
      >
        <option value="">
          All{options.length ? ` (${options.reduce((n, o) => n + o.markets, 0)} markets)` : ""}
        </option>
        {options.map((option) => (
          <option key={option.psgc_code} value={option.psgc_code}>
            {option.name} ({option.markets})
          </option>
        ))}
      </select>
    </label>
  );
}
