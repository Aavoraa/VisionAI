"use client";

import { ArrowRight, RefreshCw } from "lucide-react";
import Link from "next/link";

import { QuotaCard } from "@/components/billing";
import { Alert, Button, Card, PageHeader, Skeleton, StatCard } from "@/components/ui";
import { api, unwrap } from "@/lib/api/client";
import { formatCompact, formatDate, formatUsd } from "@/lib/format";
import { useApiQuery } from "@/lib/use-api";

const PUBLIC_API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

function QuickStartCard() {
  return (
    <Card title="Send a request" description="Call the API with one of your keys.">
      <pre className="overflow-x-auto rounded-lg bg-surface-muted p-4 font-mono text-xs leading-relaxed text-fg">
        {`curl ${PUBLIC_API_URL}/v1/vision \\
  -H "Authorization: Bearer $VISION_API_KEY" \\
  -F prompt="Describe this image" \\
  -F images=@photo.jpg`}
      </pre>
      <Link
        href="/keys"
        className="mt-4 inline-flex items-center gap-1 text-sm font-medium text-primary underline-offset-4 hover:underline"
      >
        Create or manage API keys
        <ArrowRight aria-hidden className="size-4" />
      </Link>
    </Card>
  );
}

function OverviewSkeleton() {
  return (
    <div aria-busy="true" aria-label="Loading overview">
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        {[0, 1, 2, 3].map((i) => (
          <Skeleton key={i} className="h-[104px]" />
        ))}
      </div>
      <div className="mt-6 grid gap-6 lg:grid-cols-2">
        <Skeleton className="h-48" />
        <Skeleton className="h-48" />
      </div>
    </div>
  );
}

export default function OverviewPage() {
  const billing = useApiQuery(() => unwrap(api.GET("/v1/dashboard/billing")));

  return (
    <>
      <PageHeader
        title="Overview"
        description={
          billing.status === "success"
            ? `Usage for ${formatDate(billing.data.period.start, { month: "long", year: "numeric" })} (UTC).`
            : "Usage for the current billing month (UTC)."
        }
      />
      {billing.status === "loading" && <OverviewSkeleton />}
      {billing.status === "error" && (
        <div className="flex flex-col items-start gap-3">
          <Alert>Could not load your usage: {billing.error.message}</Alert>
          <Button variant="secondary" onClick={billing.reload}>
            <RefreshCw aria-hidden className="size-4" />
            Try again
          </Button>
        </div>
      )}
      {billing.status === "success" && (
        <>
          <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
            <StatCard
              label="Requests this month"
              value={formatCompact(billing.data.usage.requests)}
              detail={`${formatCompact(billing.data.usage.images)} images`}
            />
            <StatCard label="Spend this month" value={formatUsd(billing.data.cost.total_usd)} detail="Failed requests are free" />
            <StatCard label="Projected month-end" value={formatUsd(billing.data.cost.projected_usd)} detail="At the current daily rate" />
            <StatCard
              label="Tokens this month"
              value={formatCompact(billing.data.usage.input_tokens + billing.data.usage.output_tokens)}
              detail={`${formatCompact(billing.data.usage.input_tokens)} in · ${formatCompact(billing.data.usage.output_tokens)} out`}
            />
          </div>
          <div className="mt-6 grid gap-6 lg:grid-cols-2">
            <QuotaCard billing={billing.data} />
            <QuickStartCard />
          </div>
        </>
      )}
    </>
  );
}
