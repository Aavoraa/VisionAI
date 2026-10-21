"use client";

import { RefreshCw } from "lucide-react";

import { QuotaCard } from "@/components/billing";
import { MonthlyCostChart, toMonthlyPoints } from "@/components/charts";
import { Alert, Button, Card, PageHeader, Skeleton, StatCard } from "@/components/ui";
import { api, unwrap, type BillingOverview } from "@/lib/api/client";
import { formatDate, formatNumber, formatUnitPrice, formatUsd } from "@/lib/format";
import { useApiQuery } from "@/lib/use-api";

function PlanCard({ plan }: { plan: BillingOverview["plan"] }) {
  const rows: Array<[string, string]> = [
    ["Input tokens", `${formatUnitPrice(plan.price_per_1k_input_tokens_usd)} per 1K`],
    ["Output tokens", `${formatUnitPrice(plan.price_per_1k_output_tokens_usd)} per 1K`],
    ["Images", `${formatUnitPrice(plan.price_per_image_usd)} each`],
    ["Monthly requests", plan.monthly_request_limit === null ? "Unlimited" : formatNumber(plan.monthly_request_limit)],
    ["Monthly spend cap", plan.monthly_spend_limit_usd === null ? "None" : formatUsd(plan.monthly_spend_limit_usd)],
  ];
  return (
    <Card title={`${plan.name} plan`} description="Plans are assigned by your administrator.">
      <dl className="divide-y divide-border text-sm">
        {rows.map(([label, value]) => (
          <div key={label} className="flex justify-between gap-4 py-2.5 first:pt-0 last:pb-0">
            <dt className="text-muted">{label}</dt>
            <dd className="font-mono tabular-nums text-fg">{value}</dd>
          </div>
        ))}
      </dl>
    </Card>
  );
}

export default function BillingPage() {
  const billing = useApiQuery(() => unwrap(api.GET("/v1/dashboard/billing")));

  return (
    <>
      <PageHeader
        title="Billing"
        description="Charges are calculated per request at your plan's prices. Failed requests are free."
      />
      {billing.status === "loading" && (
        <div aria-busy="true" aria-label="Loading billing" className="grid gap-6 lg:grid-cols-3">
          <Skeleton className="h-40" />
          <Skeleton className="h-40" />
          <Skeleton className="h-40" />
        </div>
      )}
      {billing.status === "error" && (
        <div className="flex flex-col items-start gap-3">
          <Alert>Could not load billing: {billing.error.message}</Alert>
          <Button variant="secondary" onClick={billing.reload}>
            <RefreshCw aria-hidden className="size-4" />
            Try again
          </Button>
        </div>
      )}
      {billing.status === "success" && (
        <div className="flex flex-col gap-6">
          <div className="grid gap-4 sm:grid-cols-3">
            <StatCard
              label="Charges this month"
              value={formatUsd(billing.data.cost.total_usd)}
              detail={`Since ${formatDate(billing.data.period.start, { dateStyle: "medium", timeZone: "UTC" })}`}
            />
            <StatCard
              label="Projected month-end"
              value={formatUsd(billing.data.cost.projected_usd)}
              detail="At the current daily rate"
            />
            <StatCard
              label="Requests this month"
              value={formatNumber(billing.data.usage.requests)}
              detail={`Resets ${formatDate(billing.data.period.resets_at)}`}
            />
          </div>
          <div className="grid gap-6 lg:grid-cols-2">
            <PlanCard plan={billing.data.plan} />
            <QuotaCard billing={billing.data} />
          </div>
          <Card title="Monthly charges" description="The last six months, including the current one.">
            <MonthlyCostChart points={toMonthlyPoints(billing.data.history)} />
          </Card>
        </div>
      )}
    </>
  );
}
