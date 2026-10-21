"use client";

import { useId, useState, type ReactNode } from "react";
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { DataTable, type Column } from "@/components/ui";
import type { BillingOverview, UsageReport } from "@/lib/api/client";
import { formatCompact, formatDay, formatMonth, formatNumber, formatUsd, formatUsdTick } from "@/lib/format";

// Colours come from the design tokens, so charts follow light/dark mode automatically.
// Blue + amber stay distinguishable for the common forms of colour blindness.
const PRIMARY = "var(--primary)";
const ACCENT = "var(--accent)";
const GRID = "var(--border)";
const AXIS = { fontSize: 12, fill: "var(--muted)" };
const TOOLTIP_STYLE = {
  background: "var(--surface)",
  border: "1px solid var(--border)",
  borderRadius: 8,
  color: "var(--fg)",
  fontSize: 13,
};

export type DailyPoint = { date: string; successful: number; failed: number; cost: number };
export type MonthlyPoint = { month: string; requests: number; cost: number };

/** Works for both the user's and the platform's usage report (same daily shape). */
export function toDailyPoints(report: Pick<UsageReport, "daily">): DailyPoint[] {
  return report.daily.map((day) => ({
    date: day.date,
    successful: day.requests - day.failed,
    failed: day.failed,
    cost: Number(day.cost_usd),
  }));
}

export function toMonthlyPoints(history: BillingOverview["history"]): MonthlyPoint[] {
  return history.map((month) => ({ month: month.month, requests: month.requests, cost: Number(month.cost_usd) }));
}

/** Chart with a text summary for screen readers and an on-demand table of the same data. */
function ChartFrame<T>({
  summary,
  columns,
  rows,
  rowKey,
  caption,
  children,
}: {
  summary: string;
  columns: Column<T>[];
  rows: T[];
  rowKey: (row: T) => string;
  caption: string;
  children: ReactNode;
}) {
  const [showTable, setShowTable] = useState(false);
  const tableId = useId();
  return (
    <div>
      <div role="img" aria-label={summary} className="h-64 w-full">
        {children}
      </div>
      <button
        type="button"
        aria-expanded={showTable}
        aria-controls={tableId}
        onClick={() => setShowTable((value) => !value)}
        className="mt-2 cursor-pointer text-sm font-medium text-primary underline-offset-4 hover:underline"
      >
        {showTable ? "Hide data table" : "Show data table"}
      </button>
      <div id={tableId} hidden={!showTable} className="mt-3">
        <DataTable columns={columns} rows={rows} rowKey={rowKey} caption={caption} />
      </div>
    </div>
  );
}

const dailyColumns: Column<DailyPoint>[] = [
  { header: "Day (UTC)", cell: (row) => formatDay(row.date, { month: "short", day: "numeric", year: "numeric" }) },
  { header: "Successful", cell: (row) => formatNumber(row.successful), align: "right" },
  { header: "Failed", cell: (row) => formatNumber(row.failed), align: "right" },
  { header: "Spend", cell: (row) => formatUsd(row.cost), align: "right" },
];

export function RequestsChart({ points }: { points: DailyPoint[] }) {
  const total = points.reduce((sum, p) => sum + p.successful + p.failed, 0);
  const failed = points.reduce((sum, p) => sum + p.failed, 0);
  return (
    <ChartFrame
      summary={`Requests per day: ${formatNumber(total)} in total, ${formatNumber(failed)} failed.`}
      columns={dailyColumns}
      rows={points}
      rowKey={(row) => row.date}
      caption="Requests and spend per day"
    >
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={points} margin={{ top: 8, right: 8, bottom: 0, left: -12 }}>
          <CartesianGrid vertical={false} stroke={GRID} />
          <XAxis dataKey="date" tickFormatter={(d: string) => formatDay(d)} tick={AXIS} tickLine={false} axisLine={false} minTickGap={16} />
          <YAxis allowDecimals={false} tickFormatter={formatCompact} tick={AXIS} tickLine={false} axisLine={false} />
          <Tooltip
            contentStyle={TOOLTIP_STYLE}
            cursor={{ fill: "var(--surface-muted)" }}
            labelFormatter={(d) => formatDay(String(d), { month: "short", day: "numeric", year: "numeric" })}
            formatter={(value, name) => [formatNumber(Number(value)), name]}
          />
          <Legend wrapperStyle={{ fontSize: 13 }} />
          <Bar dataKey="successful" name="Successful" stackId="requests" fill={PRIMARY} radius={[0, 0, 0, 0]} />
          <Bar dataKey="failed" name="Failed" stackId="requests" fill={ACCENT} radius={[3, 3, 0, 0]} />
        </BarChart>
      </ResponsiveContainer>
    </ChartFrame>
  );
}

export function SpendChart({ points }: { points: DailyPoint[] }) {
  const gradientId = useId().replace(/:/g, "");
  const total = points.reduce((sum, p) => sum + p.cost, 0);
  return (
    <ChartFrame
      summary={`Spend per day, ${formatUsd(total)} in total.`}
      columns={dailyColumns}
      rows={points}
      rowKey={(row) => row.date}
      caption="Requests and spend per day"
    >
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={points} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
          <defs>
            <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={PRIMARY} stopOpacity={0.3} />
              <stop offset="100%" stopColor={PRIMARY} stopOpacity={0} />
            </linearGradient>
          </defs>
          <CartesianGrid vertical={false} stroke={GRID} />
          <XAxis dataKey="date" tickFormatter={(d: string) => formatDay(d)} tick={AXIS} tickLine={false} axisLine={false} minTickGap={16} />
          <YAxis tickFormatter={formatUsdTick} tick={AXIS} tickLine={false} axisLine={false} width={64} />
          <Tooltip
            contentStyle={TOOLTIP_STYLE}
            labelFormatter={(d) => formatDay(String(d), { month: "short", day: "numeric", year: "numeric" })}
            formatter={(value) => [formatUsd(Number(value)), "Spend"]}
          />
          <Area type="monotone" dataKey="cost" name="Spend" stroke={PRIMARY} strokeWidth={2} fill={`url(#${gradientId})`} />
        </AreaChart>
      </ResponsiveContainer>
    </ChartFrame>
  );
}

const monthlyColumns: Column<MonthlyPoint>[] = [
  { header: "Month", cell: (row) => formatMonth(row.month) },
  { header: "Requests", cell: (row) => formatNumber(row.requests), align: "right" },
  { header: "Charges", cell: (row) => formatUsd(row.cost), align: "right" },
];

export function MonthlyCostChart({ points }: { points: MonthlyPoint[] }) {
  return (
    <ChartFrame
      summary={`Charges for the last ${points.length} months: ${points
        .map((p) => `${formatMonth(p.month)} ${formatUsd(p.cost)}`)
        .join(", ")}.`}
      columns={monthlyColumns}
      rows={points}
      rowKey={(row) => row.month}
      caption="Charges per month"
    >
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={points} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
          <CartesianGrid vertical={false} stroke={GRID} />
          <XAxis dataKey="month" tickFormatter={(m: string) => formatMonth(m)} tick={AXIS} tickLine={false} axisLine={false} />
          <YAxis tickFormatter={formatUsdTick} tick={AXIS} tickLine={false} axisLine={false} width={64} />
          <Tooltip
            contentStyle={TOOLTIP_STYLE}
            cursor={{ fill: "var(--surface-muted)" }}
            labelFormatter={(m) => formatMonth(String(m))}
            formatter={(value) => [formatUsd(Number(value)), "Charges"]}
          />
          <Bar dataKey="cost" name="Charges" fill={PRIMARY} radius={[3, 3, 0, 0]} />
        </BarChart>
      </ResponsiveContainer>
    </ChartFrame>
  );
}
