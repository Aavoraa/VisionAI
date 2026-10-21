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

const keyColumns: Column<Schemas["KeyUsage"]>[] = [
  {
    header: "Key",
    cell: (row) => (
      <div>
        <p className="font-medium">{row.name}</p>
        <p className="font-mono text-xs text-muted">{row.prefix}…</p>
      </div>
    ),
  },
  { header: "Requests", cell: (row) => formatNumber(row.requests), align: "right" },
  { header: "Input tokens", cell: (row) => formatNumber(row.input_tokens), align: "right", hideBelow: "md" },
  { header: "Output tokens", cell: (row) => formatNumber(row.output_tokens), align: "right", hideBelow: "md" },
];

function UsageSkeleton() {
  return (
    <div aria-busy="true" aria-label="Loading usage" className="flex flex-col gap-6">
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        {[0, 1, 2, 3].map((i) => (
          <Skeleton key={i} className="h-[104px]" />
        ))}
      </div>
      <Skeleton className="h-80" />
    </div>
  );
}

export default function UsagePage() {
  const [days, setDays] = useState(30);
  const usage = useApiQuery(
    () => unwrap(api.GET("/v1/dashboard/usage", { params: { query: { days } } })),
    String(days),
  );

  return (
    <>
      <PageHeader
        title="Usage"
        description={
          usage.status === "success"
            ? `${formatDay(usage.data.start, { month: "short", day: "numeric", year: "numeric" })} to ${formatDay(
                usage.data.end,
                { month: "short", day: "numeric", year: "numeric" },
              )} (UTC). Rate-limited and unauthenticated requests are not counted.`
            : "Requests, tokens and spend over time (UTC)."
        }
        action={<Segmented label="Time range" options={RANGES} value={days} onChange={setDays} />}
      />

      {usage.status === "loading" && <UsageSkeleton />}
      {usage.status === "error" && (
        <div className="flex flex-col items-start gap-3">
          <Alert>Could not load usage: {usage.error.message}</Alert>
          <Button variant="secondary" onClick={usage.reload}>
            <RefreshCw aria-hidden className="size-4" />
            Try again
          </Button>
        </div>
      )}
      {usage.status === "success" && (
        <div className={`flex flex-col gap-6 transition-opacity duration-200 ${usage.refreshing ? "opacity-60" : ""}`} aria-busy={usage.refreshing}>
          <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
            <StatCard
              label="Requests"
              value={formatCompact(usage.data.totals.requests)}
              detail={`${formatNumber(usage.data.totals.failed)} failed`}
            />
            <StatCard
              label="Success rate"
              value={formatPercent(successRate(usage.data.totals))}
              detail={
                usage.data.totals.avg_processing_ms === null
                  ? "No requests yet"
                  : `Avg. ${formatNumber(Math.round(usage.data.totals.avg_processing_ms))} ms per request`
              }
            />
            <StatCard
              label="Tokens"
              value={formatCompact(usage.data.totals.input_tokens + usage.data.totals.output_tokens)}
              detail={`${formatCompact(usage.data.totals.images)} images`}
            />
            <StatCard label="Spend" value={formatUsd(usage.data.totals.cost_usd)} detail="Failed requests are free" />
          </div>

          <div className="grid gap-6 xl:grid-cols-2">
            <Card title="Requests per day" description="Successful and failed requests.">
              <RequestsChart points={toDailyPoints(usage.data)} />
            </Card>
            <Card title="Spend per day" description="Charges at the prices in force when each request ran.">
              <SpendChart points={toDailyPoints(usage.data)} />
            </Card>
          </div>

          <div className="grid gap-6 xl:grid-cols-2">
            <Card title="By model">
              <DataTable caption="Usage by model" columns={modelColumns} rows={usage.data.by_model} rowKey={(r) => r.model} empty="No requests in this period." />
            </Card>
            <Card title="By API key">
              <DataTable caption="Usage by API key" columns={keyColumns} rows={usage.data.by_key} rowKey={(r) => r.api_key_id} empty="No requests in this period." />
            </Card>
          </div>
        </div>
      )}
    </>
  );
}
