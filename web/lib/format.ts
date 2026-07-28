/** Formatting helpers. Every one of them has to survive missing data, because gaps are real. */

/** A peso amount, from the API's exact decimal string. `null` renders as an em dash. */
export function peso(value: string | null): string {
  if (value === null) return "—";
  return `₱${Number(value).toLocaleString("en-PH", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

/** A signed percentage, for a movers row. */
export function percent(value: number): string {
  return `${value > 0 ? "+" : ""}${value.toFixed(1)}%`;
}

/** `2026-07-28` as `28 Jul`. Charts have no room for the year on every tick. */
export function shortDate(iso: string): string {
  const [, month, day] = iso.split("-");
  const months = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
  ];
  return `${Number(day)} ${months[Number(month) - 1] ?? month}`;
}

/** `2026-07-28` as `28 July 2026`. */
export function longDate(iso: string): string {
  return new Date(`${iso}T00:00:00Z`).toLocaleDateString("en-GB", {
    day: "numeric",
    month: "long",
    year: "numeric",
    timeZone: "UTC",
  });
}

/** The date `days` before today, as an ISO date. */
export function daysAgo(days: number): string {
  const when = new Date();
  when.setUTCDate(when.getUTCDate() - days);
  return when.toISOString().slice(0, 10);
}

export function commodityLabel(name: string, specification: string | null): string {
  return specification ? `${name} (${specification})` : name;
}
