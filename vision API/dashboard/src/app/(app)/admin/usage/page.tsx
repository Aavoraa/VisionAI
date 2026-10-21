"use client";

import { RefreshCw } from "lucide-react";
import { useState } from "react";

import { RequestsChart, SpendChart, toDailyPoints } from "@/components/charts";
import { Alert, Button, Card, DataTable, PageHeader, Segmented, Skeleton, StatCard, type Column } from "@/components/ui";
import { api, unwrap, type Schemas } from "@/lib/api/client";
import { formatCompact, formatDay, formatNumber, formatPercent, formatUsd, successRate } from "@/lib/format";
import { useApiQuery } from "@/lib/use-api";

const RANGES = [
  { value: 7, label: "7 days" },
  { value: 30, label: "30 days" },
  { value: 90, label: "90 days" },
];

const modelColumns: Column<Schemas["ModelUsage"]>[] = [
  { header: "Model", cell: (row) => <span className="font-mono text-xs">{row.model}</span> },
  { header: "Requests", cell: (row) => formatNumber(row.requests), align: "right" },
  { header: "Input tokens", cell: (row) => formatNumber(row.input_tokens), align: "right", hideBelow: "md" },
  { header: "Output tokens", cell: (row) => formatNumber(row.output_tokens), align: "right", hideBelow: "md" },
];

const userColumns: Column<Schemas["UserUsage"]>[] = [
  { header: "User", cell: (row) => row.email },
  { header: "Requests", cell: (row) => formatNumber(row.requests), align: "right" },
  {
    header: "Tokens",
    cell: (row) => formatNumber(row.input_tokens + row.output_tokens),
    align: "right",
    hideBelow: "md",
  },
];

export default function PlatformUsagePage() {
  const [days, setDays] = useState(30);
  const usage = useApiQuery(
    () => unwrap(api.GET("/v1/admin/usage", { params: { query: { days } } })),
    String(days),
  );

  return (
    <>
      <PageHeader
        title="Platform usage"
        description={
          usage.status === "success"
            ? `All users, ${formatDay(usage.data.start, { month: "short", day: "numeric", year: "numeric" })} to ${formatDay(
                usage.data.end,
                { month: "short", day: "numeric", year: "numeric" },
              )} (UTC).`
            : "Traffic and charges across all users (UTC)."
        }
        action={<Segmented label="Time range" options={RANGES} value={days} onChange={setDays} />}
      />
      {usage.status === "loading" && <Skeleton className="h-96" />}
      {usage.status === "error" && (
        <div className="flex flex-col items-start gap-3">
          <Alert>Could not load platform usage: {usage.error.message}</Alert>
          <Button variant="secondary" onClick={usage.reload}>
            <RefreshCw aria-hidden className="size-4" />
            Try again
          </Button>
        </div>
      )}
      {usage.status === "success" && (
        <div className={`flex flex-col gap-6 transition-opacity duration-200 ${usage.refreshing ? "opacity-60" : ""}`} aria-busy={usage.refreshing}>
          <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
            <StatCard label="Requests" value={formatCompact(usage.data.totals.requests)} detail={`${formatNumber(usage.data.totals.failed)} failed`} />
            <StatCard label="Active users" value={formatNumber(usage.data.active_users)} detail="With at least one request" />
            <StatCard label="Success rate" value={formatPercent(successRate(usage.data.totals))} detail={`${formatCompact(usage.data.totals.images)} images`} />
            <StatCard label="Charges" value={formatUsd(usage.data.totals.cost_usd)} detail="All users, this period" />
          </div>
          <div className="grid gap-6 xl:grid-cols-2">
            <Card title="Requests per day" description="Successful and failed requests, all users.">
              <RequestsChart points={toDailyPoints(usage.data)} />
            </Card>
            <Card title="Charges per day" description="At the prices in force when each request ran.">
              <SpendChart points={toDailyPoints(usage.data)} />
            </Card>
          </div>
          <div className="grid gap-6 xl:grid-cols-2">
            <Card title="Top users">
              <DataTable caption="Top users by requests" columns={userColumns} rows={usage.data.top_users} rowKey={(r) => r.user_id} empty="No requests in this period." />
            </Card>
            <Card title="By model">
              <DataTable caption="Platform usage by model" columns={modelColumns} rows={usage.data.by_model} rowKey={(r) => r.model} empty="No requests in this period." />
            </Card>
          </div>
        </div>
      )}
    </>
  );
}
