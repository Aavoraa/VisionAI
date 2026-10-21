import { Card, UsageBar } from "@/components/ui";
import type { BillingOverview } from "@/lib/api/client";
import { formatDate, formatNumber, formatUsd, usageRatio } from "@/lib/format";

export function QuotaCard({ billing }: { billing: BillingOverview }) {
  const { quota } = billing;
  const requestRatio = usageRatio(quota.requests_used, quota.request_limit);
  const spendLimit = quota.spend_limit_usd === null ? null : Number(quota.spend_limit_usd);
  const spendRatio = usageRatio(Number(quota.spend_used_usd), spendLimit);
  return (
    <Card title="Monthly allowance" description={`Plan: ${billing.plan.name}. Resets ${formatDate(billing.period.resets_at)}.`}>
      <div className="flex flex-col gap-5">
        {requestRatio === null ? (
          <p className="text-sm text-muted">Requests: unlimited on this plan.</p>
        ) : (
          <UsageBar
            label="Requests"
            ratio={requestRatio}
            valueText={`${formatNumber(quota.requests_used)} of ${formatNumber(quota.request_limit ?? 0)}`}
          />
        )}
        {spendRatio === null ? (
          <p className="text-sm text-muted">Spend: no monthly cap on this plan.</p>
        ) : (
          <UsageBar
            label="Spend"
            ratio={spendRatio}
            valueText={`${formatUsd(quota.spend_used_usd)} of ${formatUsd(quota.spend_limit_usd)}`}
          />
        )}
        {(requestRatio !== null && requestRatio >= 1) || (spendRatio !== null && spendRatio >= 1) ? (
          <p className="text-sm text-danger">
            New API requests are refused (HTTP 402) until {formatDate(billing.period.resets_at)}.
          </p>
        ) : null}
      </div>
    </Card>
  );
}
