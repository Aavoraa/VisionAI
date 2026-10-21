const integer = new Intl.NumberFormat("en-US");
const compact = new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 });

export function formatNumber(value: number): string {
  return integer.format(value);
}

export function formatCompact(value: number): string {
  return value < 10_000 ? integer.format(value) : compact.format(value);
}

/**
 * Format a USD amount given as the API's exact decimal string ("0.002144").
 * Cents for normal amounts; sub-cent amounts keep 2 significant digits so they don't read as $0.00.
 */
export function formatUsd(amount: string | number | null | undefined): string {
  if (amount === null || amount === undefined || amount === "") return "—";
  const value = typeof amount === "number" ? amount : Number(amount);
  if (!Number.isFinite(value)) return "—";
  if (value !== 0 && Math.abs(value) < 0.01) {
    return `$${value.toPrecision(2).replace(/0+$/, "").replace(/\.$/, "")}`;
  }
  return new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }).format(value);
}

/** Fraction used of a limit, 0-1 (clamped); null when unlimited. */
export function usageRatio(used: number, limit: number | null | undefined): number | null {
  if (limit === null || limit === undefined) return null;
  if (limit <= 0) return 1;
  return Math.min(1, Math.max(0, used / limit));
}

export function formatDate(iso: string, options: Intl.DateTimeFormatOptions = { dateStyle: "medium" }): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? "—" : new Intl.DateTimeFormat("en-US", options).format(date);
}

/**
 * Format a UTC calendar day from the API ("2026-09-24"). Parsed and printed in UTC so the
 * label never shifts to the previous day in time zones west of UTC.
 */
export function formatDay(day: string, options: Intl.DateTimeFormatOptions = { month: "short", day: "numeric" }): string {
  const date = new Date(`${day}T00:00:00Z`);
  return Number.isNaN(date.getTime())
    ? "—"
    : new Intl.DateTimeFormat("en-US", { ...options, timeZone: "UTC" }).format(date);
}

/** "2026-09" -> "Sep 2026" (UTC). */
export function formatMonth(month: string): string {
  return formatDay(`${month}-01`, { month: "short", year: "numeric" });
}

export function formatPercent(ratio: number | null): string {
  return ratio === null ? "—" : `${(ratio * 100).toFixed(ratio === 1 || ratio === 0 ? 0 : 1)}%`;
}

/** Only same-site relative paths are allowed as post-login destinations (no open redirects). */
export function safeNextPath(next: string | null | undefined, fallback = "/overview"): string {
  if (!next || !next.startsWith("/") || next.startsWith("//") || next.startsWith("/\\")) return fallback;
  if (next.startsWith("/login") || next.startsWith("/signup")) return fallback;
  return next;
}

/** Per-unit prices can be tiny (e.g. $0.0005 per 1K tokens): shown exactly, never rounded to cents. */
export function formatUnitPrice(amount: string): string {
  const value = Number(amount);
  if (!Number.isFinite(value)) return "—";
  return `$${value.toFixed(6).replace(/0+$/, "").replace(/\.$/, ".00")}`;
}

const axisUsd = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumSignificantDigits: 3,
});

/**
 * Chart-axis money: evenly spaced ticks like 0.004 / 0.008 / 0.012 must stay distinct, so
 * this keeps significant digits instead of rounding to cents (which would print 0.012 as $0.01).
 */
export function formatUsdTick(value: number): string {
  return value === 0 ? "$0" : axisUsd.format(value);
}

/** Share of requests that succeeded; null when there were none. */
export function successRate(totals: { requests: number; successful: number }): number | null {
  return totals.requests === 0 ? null : totals.successful / totals.requests;
}
