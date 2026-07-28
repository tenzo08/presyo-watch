/**
 * Typed client for the PresyoWatch API.
 *
 * Two things here are load-bearing and neither is obvious.
 *
 * **Prices arrive as strings and stay strings.** The API sends `"52.80"` rather than 52.80
 * because JSON numbers are IEEE-754 doubles and `52.80` parses to 52.79999999999999716.
 * They are converted to `number` exactly once, at the edge of the chart, where a pixel
 * position is wanted and precision no longer matters. Everything a reader *sees* as a price
 * is rendered from the original string.
 *
 * **The API is expected to be asleep.** It runs on a Render free instance that spins down
 * after 15 minutes and takes 30–60 seconds to wake. That is not an error condition to be
 * retried away; it is the normal first request of the day. So the timeout is generous, and
 * the caller is told *which* failure it got — a wake-up wait reads differently to a reader
 * than a 500 does.
 */

export const API_URL = (process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000").replace(
  /\/+$/,
  "",
);

/** Long enough for a cold Render instance to wake, short enough to eventually give up. */
const TIMEOUT_MS = 90_000;

export class ApiError extends Error {
  constructor(
    message: string,
    readonly kind: "offline" | "timeout" | "http",
    readonly status?: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export interface Commodity {
  canonical_slug: string;
  group: string;
  name: string;
  specification: string | null;
  unit: string;
  is_agricultural_input: boolean;
}

export interface Observation {
  observed_on: string;
  commodity_slug: string;
  market_id: number;
  market: string;
  municipality: string;
  region_psgc_code: string;
  source_slug: string;
  low: string | null;
  high: string | null;
  prevailing: string | null;
  average: string | null;
  unavailable: boolean;
  revision_no: number;
  ingested_at: string;
}

export interface Mover {
  commodity_slug: string;
  commodity: string;
  group: string;
  unit: string;
  market_id: number;
  market: string;
  municipality: string;
  region_psgc_code: string;
  first_observed_on: string;
  last_observed_on: string;
  first_average: string | null;
  last_average: string | null;
  change: string | null;
  percent_change: number;
  observations: number;
}

export interface Source {
  slug: string;
  name: string;
  base_url: string;
  licence: string | null;
  attribution_text: string;
}

export interface Run {
  run_id: string;
  source_slug: string;
  status: string;
  started_at: string;
  finished_at: string | null;
  files_seen: number;
  files_fetched: number;
  rows_upserted: number;
  rows_quarantined: number;
  error: string | null;
}

type Params = Record<string, string | number | boolean | undefined>;

async function get<T>(path: string, params: Params = {}, signal?: AbortSignal): Promise<T> {
  const url = new URL(API_URL + path);
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined) url.searchParams.set(key, String(value));
  }

  // Two signals, because they mean different things: the caller's (the reader navigated
  // away) and ours (the API never woke up). Merging them keeps one `fetch` call while
  // letting the catch below tell the two apart.
  const timeout = AbortSignal.timeout(TIMEOUT_MS);
  const merged = signal ? AbortSignal.any([signal, timeout]) : timeout;

  let response: Response;
  try {
    response = await fetch(url, { signal: merged, headers: { Accept: "application/json" } });
  } catch (cause) {
    if (signal?.aborted) throw cause;
    if (timeout.aborted) {
      throw new ApiError(
        "The API did not answer in 90 seconds. It sleeps when idle and is probably still waking up.",
        "timeout",
      );
    }
    throw new ApiError("Could not reach the API.", "offline");
  }

  if (!response.ok) {
    throw new ApiError(`The API answered ${response.status}.`, "http", response.status);
  }
  return (await response.json()) as T;
}

export const api = {
  commodities: (params: { q?: string; limit?: number; include_agricultural_inputs?: boolean },
                signal?: AbortSignal) =>
    get<Page<Commodity>>("/commodities", params, signal),

  commodity: (slug: string, signal?: AbortSignal) =>
    get<Commodity>(`/commodities/${encodeURIComponent(slug)}`, {}, signal),

  observations: (
    params: { commodity: string; date_from?: string; region?: string; limit?: number },
    signal?: AbortSignal,
  ) => get<Page<Observation>>("/observations", params, signal),

  movers: (params: { window_days?: number; limit?: number }, signal?: AbortSignal) =>
    get<Page<Mover>>("/movers", params, signal),

  sources: (signal?: AbortSignal) => get<Source[]>("/meta/sources", {}, signal),

  runs: (params: { limit?: number }, signal?: AbortSignal) =>
    get<Page<Run>>("/meta/runs", params, signal),
};
