"use client";

import { Download, FilePlus2 } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { Confidence, ReviewStatusBadge } from "@/components/invoice";
import { Alert, Button, Card, DataTable, PageHeader, Segmented, Skeleton, type Column } from "@/components/ui";
import { api, unwrap } from "@/lib/api/client";
import { formatDate } from "@/lib/format";
import { REVIEW_TABS, displayValue, type InvoiceSummary, type ReviewStatus } from "@/lib/invoices";
import { useApiQuery } from "@/lib/use-api";

const PAGE_SIZE = 25;

const EXPORTS = [
  { format: "csv", label: "Invoices CSV" },
  { format: "items_csv", label: "Line items CSV" },
  { format: "json", label: "JSON" },
] as const;

/** Same-origin download link (the session cookie authenticates it). */
function exportUrl(format: string, tab: ReviewStatus | "all"): string {
  const query = new URLSearchParams({ format });
  if (tab !== "all") query.set("status", tab);
  return `/api/v1/dashboard/invoices/export?${query}`;
}

const COLUMNS: Column<InvoiceSummary>[] = [
  {
    header: "Invoice",
    cell: (row) => (
      <Link href={`/invoices/${row.id}`} className="font-medium text-primary hover:underline">
        {row.invoice_number ?? row.filename}
      </Link>
    ),
  },
  { header: "Seller", cell: (row) => <span className="line-clamp-1 max-w-64">{row.vendor_name ?? "—"}</span>, hideBelow: "lg" },
  { header: "Total", cell: (row) => displayValue(row.total_amount, "money"), align: "right" },
  { header: "Confidence", cell: (row) => <Confidence value={row.confidence} />, align: "right", hideBelow: "md" },
  { header: "Status", cell: (row) => <ReviewStatusBadge status={row.status} /> },
  { header: "Uploaded", cell: (row) => formatDate(row.created_at), hideBelow: "xl" },
];

export default function InvoiceListPage() {
  const [tab, setTab] = useState<ReviewStatus | "all">("needs_review");
  const [page, setPage] = useState(0);
  const invoices = useApiQuery(
    () =>
      unwrap(
        api.GET("/v1/dashboard/invoices", {
          params: { query: { status: tab === "all" ? undefined : tab, limit: PAGE_SIZE, offset: page * PAGE_SIZE } },
        }),
      ),
    `${tab}:${page}`,
  );
  const counts = invoices.data?.counts ?? {};
  const total = invoices.data?.total ?? 0;

  return (
    <>
      <PageHeader
        title="Invoices"
        description="Invoices you extracted, by review status. Confident invoices whose checks all passed are approved automatically; the rest wait for review."
        action={
          <Link
            href="/invoices/new"
            className="inline-flex min-h-10 items-center justify-center gap-2 rounded-lg bg-primary px-4 text-sm font-medium text-primary-fg transition-colors duration-150 hover:bg-primary-hover"
          >
            <FilePlus2 aria-hidden className="size-4" />
            Extract an invoice
          </Link>
        }
      />
      <Card>
        <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
          <Segmented
            label="Review status"
            options={REVIEW_TABS.map((t) => ({
              value: t.value,
              label: t.value === "all" ? t.label : `${t.label} (${counts[t.value] ?? 0})`,
            }))}
            value={tab}
            onChange={(value) => {
              setTab(value);
              setPage(0);
            }}
          />
          <div role="group" aria-label="Export this list" className="flex flex-wrap items-center gap-2">
            {EXPORTS.map((item) => (
              <a
                key={item.format}
                href={exportUrl(item.format, tab)}
                download
                className="inline-flex min-h-9 items-center gap-1.5 rounded-lg border border-border bg-surface px-3 text-sm font-medium text-fg transition-colors duration-150 hover:bg-surface-muted"
              >
                <Download aria-hidden className="size-4" />
                {item.label}
              </a>
            ))}
          </div>
        </div>
        {(counts.processing ?? 0) + (counts.failed ?? 0) > 0 && (
          <div className="-mt-2 mb-3">
            <p className="text-xs text-muted">
              {counts.processing ?? 0} processing · {counts.failed ?? 0} failed (under All)
            </p>
          </div>
        )}
        {invoices.status === "loading" ? (
          <Skeleton className="h-48" />
        ) : invoices.status === "error" ? (
          <Alert>
            {invoices.error.message}{" "}
            <button type="button" className="cursor-pointer underline" onClick={invoices.reload}>
              Try again
            </button>
          </Alert>
        ) : (
          <>
            <DataTable
              columns={COLUMNS}
              rows={invoices.data.items}
              rowKey={(row) => row.id}
              caption="Invoices"
              empty={
                tab === "needs_review" ? "Nothing waiting for review." : (
                  <>
                    No invoices here yet. <Link href="/invoices/new" className="text-primary underline">Extract one</Link>.
                  </>
                )
              }
            />
            {total > PAGE_SIZE && (
              <div className="mt-4 flex items-center justify-between text-sm text-muted">
                <span>
                  {page * PAGE_SIZE + 1}–{Math.min(total, (page + 1) * PAGE_SIZE)} of {total}
                </span>
                <div className="flex gap-2">
                  <Button variant="secondary" disabled={page === 0} onClick={() => setPage((p) => p - 1)}>
                    Previous
                  </Button>
                  <Button variant="secondary" disabled={(page + 1) * PAGE_SIZE >= total} onClick={() => setPage((p) => p + 1)}>
                    Next
                  </Button>
                </div>
              </div>
            )}
          </>
        )}
      </Card>
    </>
  );
}
