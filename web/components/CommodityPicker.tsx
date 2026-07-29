"use client";

/**
 * Choosing a commodity, by searching or by browsing.
 *
 * Search alone assumed the reader already knows what the source publishes, and mostly they
 * do not — "Habichuelas", "Galunggong" and "Alumahan" are hard to search for if you have not
 * seen them. So the whole vocabulary is browsable, grouped exactly as the source groups it,
 * and the search box narrows that same list rather than being a separate mode.
 *
 * The full list is fetched once and filtered in the browser. It is about 150 rows, which is
 * smaller than a single day of prices, and it makes typing feel instant while the API is
 * still waking up. Searching the server instead would put a 30-second cold start between a
 * reader and their first keystroke.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { api, type Commodity } from "@/lib/api";
import { commodityLabel } from "@/lib/format";

export function CommodityPicker({
  onSelect,
  selected,
}: {
  onSelect: (commodity: Commodity) => void;
  selected: Commodity | null;
}) {
  const [all, setAll] = useState<Commodity[] | null>(null);
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const [showInputs, setShowInputs] = useState(false);
  const panel = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const controller = new AbortController();
    api
      .commodities({ limit: 500 }, controller.signal)
      .then((page) => setAll(page.items))
      .catch(() => setAll(null));
    return () => controller.abort();
  }, []);

  useEffect(() => {
    function away(event: MouseEvent) {
      if (panel.current && !panel.current.contains(event.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", away);
    return () => document.removeEventListener("mousedown", away);
  }, []);

  const groups = useMemo(() => {
    if (!all) return [];
    const needle = query.trim().toLowerCase();
    const matching = all.filter((item) => {
      if (!showInputs && item.is_agricultural_input) return false;
      if (!needle) return true;
      return (
        item.name.toLowerCase().includes(needle) ||
        item.group.toLowerCase().includes(needle) ||
        (item.specification ?? "").toLowerCase().includes(needle)
      );
    });

    const byGroup = new Map<string, Commodity[]>();
    for (const item of matching) {
      const bucket = byGroup.get(item.group);
      if (bucket) bucket.push(item);
      else byGroup.set(item.group, [item]);
    }
    return [...byGroup.entries()].sort(([a], [b]) => a.localeCompare(b));
  }, [all, query, showInputs]);

  const total = groups.reduce((count, [, items]) => count + items.length, 0);

  return (
    <div className="field" ref={panel}>
      <div style={{ display: "flex", gap: 6 }}>
        <input
          type="search"
          aria-label="Search commodities"
          placeholder={
            selected ? commodityLabel(selected.name, selected.specification) : "Search…"
          }
          value={query}
          onChange={(event) => {
            setQuery(event.target.value);
            setOpen(true);
          }}
          onFocus={() => setOpen(true)}
          onKeyDown={(event) => event.key === "Escape" && setOpen(false)}
        />
        <button
          type="button"
          aria-expanded={open}
          onClick={() => setOpen((shown) => !shown)}
          title="Every commodity the source publishes"
        >
          {open ? "Close" : `Browse${all ? ` (${all.length})` : ""}`}
        </button>
      </div>

      {open ? (
        <div className="results" role="dialog" aria-label="All commodities">
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              gap: 8,
              padding: "8px 10px",
              borderBottom: "1px solid var(--border)",
              position: "sticky",
              top: 0,
              background: "var(--surface-1)",
            }}
          >
            <span className="group">
              {all === null ? "Loading…" : `${total} shown`}
            </span>
            <label className="group" style={{ display: "flex", gap: 6, alignItems: "center" }}>
              <input
                type="checkbox"
                checked={showInputs}
                onChange={(event) => setShowInputs(event.target.checked)}
              />
              Include feeds &amp; chemicals
            </label>
          </div>

          {groups.map(([group, items]) => (
            <div key={group}>
              <div
                className="group"
                style={{
                  padding: "6px 10px 2px",
                  fontWeight: 600,
                  letterSpacing: "0.03em",
                }}
              >
                {group}
              </div>
              <ul style={{ listStyle: "none", margin: 0, padding: 0 }}>
                {items.map((item) => (
                  <li key={item.canonical_slug}>
                    <button
                      type="button"
                      onClick={() => {
                        onSelect(item);
                        setQuery("");
                        setOpen(false);
                      }}
                      aria-current={item.canonical_slug === selected?.canonical_slug}
                      style={{
                        fontWeight:
                          item.canonical_slug === selected?.canonical_slug ? 600 : 400,
                      }}
                    >
                      {commodityLabel(item.name, item.specification)}
                      <span className="group">per {item.unit}</span>
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          ))}

          {all !== null && total === 0 ? (
            <p className="group" style={{ padding: "12px 10px" }}>
              Nothing matches “{query}”.
              {!showInputs ? " Feeds and chemicals are hidden — tick the box above." : ""}
            </p>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
