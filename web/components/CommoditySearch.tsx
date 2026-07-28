"use client";

/**
 * Commodity search.
 *
 * Queries the API rather than filtering a list held in the browser: the canonical list is
 * about 150 rows today and will grow as aliases are curated, and `ilike` in Postgres is
 * cheaper than shipping the whole vocabulary to every reader.
 *
 * Typing is debounced and every superseded request is aborted, so a slow answer for "ri"
 * can never arrive after the answer for "rice" and overwrite it. That race is invisible on
 * a fast connection and constant on a sleeping one.
 */

import { useEffect, useId, useRef, useState } from "react";
import { api, type Commodity } from "@/lib/api";
import { commodityLabel } from "@/lib/format";

const DEBOUNCE_MS = 220;

export function CommoditySearch({
  onSelect,
  selected,
}: {
  onSelect: (commodity: Commodity) => void;
  selected: Commodity | null;
}) {
  const [query, setQuery] = useState("");
  const [matches, setMatches] = useState<Commodity[]>([]);
  const [open, setOpen] = useState(false);
  const listId = useId();
  const box = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (query.trim().length < 2) {
      setMatches([]);
      return;
    }
    const controller = new AbortController();
    const timer = setTimeout(() => {
      api
        .commodities({ q: query.trim(), limit: 12 }, controller.signal)
        .then((page) => {
          setMatches(page.items);
          setOpen(true);
        })
        .catch(() => {
          /* A failed search is not worth an alert; the reader can retype. */
        });
    }, DEBOUNCE_MS);

    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [query]);

  useEffect(() => {
    function away(event: MouseEvent) {
      if (box.current && !box.current.contains(event.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", away);
    return () => document.removeEventListener("mousedown", away);
  }, []);

  return (
    <div className="field" ref={box}>
      <label htmlFor={`${listId}-input`} style={{ position: "absolute", left: -9999 }}>
        Search commodities
      </label>
      <input
        id={`${listId}-input`}
        type="search"
        role="combobox"
        aria-expanded={open}
        aria-controls={listId}
        aria-autocomplete="list"
        placeholder={selected ? commodityLabel(selected.name, selected.specification) : "Search a commodity — rice, bangus, tomato…"}
        value={query}
        onChange={(event) => setQuery(event.target.value)}
        onFocus={() => matches.length > 0 && setOpen(true)}
        onKeyDown={(event) => event.key === "Escape" && setOpen(false)}
      />
      {open && matches.length > 0 ? (
        <ul className="results" id={listId} role="listbox">
          {matches.map((commodity) => (
            <li key={commodity.canonical_slug} role="option" aria-selected={false}>
              <button
                type="button"
                onClick={() => {
                  onSelect(commodity);
                  setQuery("");
                  setOpen(false);
                }}
              >
                {commodityLabel(commodity.name, commodity.specification)}
                <span className="group">
                  {commodity.group}
                  {commodity.is_agricultural_input ? " · farm input" : ""} · per {commodity.unit}
                </span>
              </button>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
