"use client";

import { AlertTriangle, ArrowLeft, CheckCheck, RefreshCw, Save, Trash2 } from "lucide-react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useId, useState } from "react";

import { Dialog } from "@/components/dialog";
import { ChecksCard, Confidence, ReviewStatusBadge, StatusBadge } from "@/components/invoice";
import { Alert, Button, Card, PageHeader, Skeleton } from "@/components/ui";
import { ApiError, api, unwrap } from "@/lib/api/client";
import { formatDate } from "@/lib/format";
import {
  FIELD_GROUPS,
  changedFields,
  field,
  inputValue,
  type ExtractedField,
  type Invoice,
  type InvoiceDetail,
} from "@/lib/invoices";
import { useApiQuery } from "@/lib/use-api";

type Action = "save" | "approve" | "reprocess" | "delete";

const ITEM_FIELDS: Array<{ key: string; label: string; numeric?: boolean; width: string }> = [
  { key: "line_number", label: "#", numeric: true, width: "w-14" },
  { key: "description", label: "Description", width: "min-w-48" },
  { key: "design", label: "Design", width: "w-20" },
  { key: "size", label: "Size", width: "w-28" },
  { key: "hsn_sac", label: "HSN/SAC", width: "w-24" },
  { key: "quantity", label: "Qty", numeric: true, width: "w-20" },
  { key: "unit", label: "Unit", width: "w-16" },
  { key: "rate", label: "Rate", numeric: true, width: "w-24" },
  { key: "mrp", label: "MRP", numeric: true, width: "w-24" },
  { key: "discount_percent", label: "Disc %", numeric: true, width: "w-16" },
  { key: "taxable_value", label: "Amount", numeric: true, width: "w-28" },
];

// ---------------------------------------------------------------------------
// Document viewer
// ---------------------------------------------------------------------------

function PageViewer({ invoiceId, pages }: { invoiceId: string; pages: number }) {
  const [page, setPage] = useState(1);
  const src = `/api/v1/dashboard/invoices/${invoiceId}/pages/${page}`;
  return (
    <Card
      title="Document"
      description={pages > 1 ? `Page ${page} of ${pages}` : undefined}
      action={
        <a href={src} target="_blank" rel="noopener" className="text-sm font-medium text-primary hover:underline">
          Full size
        </a>
      }
    >
      {pages > 1 && (
        <div role="group" aria-label="Pages" className="mb-3 flex flex-wrap gap-1">
          {Array.from({ length: pages }, (_, i) => i + 1).map((n) => (
            <Button key={n} variant={n === page ? "primary" : "secondary"} aria-pressed={n === page} onClick={() => setPage(n)}>
              {n}
            </Button>
          ))}
        </div>
      )}
      <div className="max-h-[80vh] overflow-auto rounded-lg border border-border bg-surface-muted">
        {/* eslint-disable-next-line @next/next/no-img-element -- private, session-authenticated image */}
        <img src={src} alt={`Invoice page ${page}`} className="w-full" />
      </div>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Editing
// ---------------------------------------------------------------------------

function FieldEditor({
  label,
  cell,
  value,
  onChange,
  disabled,
}: {
  label: string;
  cell: ExtractedField;
  value: string;
  onChange: (value: string) => void;
  disabled: boolean;
}) {
  const id = useId();
  const status = cell.status ?? "missing";
  const edited = value.trim() !== inputValue(cell).trim();
  return (
    <div className="flex flex-col gap-1 border-b border-border py-2.5 last:border-b-0">
      <div className="flex items-center justify-between gap-2">
        <label htmlFor={id} className="text-sm text-muted">
          {label}
        </label>
        <span className="flex items-center gap-2 text-xs">
          <Confidence value={cell.confidence} />
          {edited ? <span className="font-medium text-primary">Edited</span> : <StatusBadge status={status} />}
        </span>
      </div>
      <input
        id={id}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        disabled={disabled}
        placeholder="Not on the invoice"
        aria-describedby={cell.note ? `${id}-note` : undefined}
        className={[
          "min-h-9 rounded-lg border bg-surface px-3 text-sm text-fg transition-colors duration-150",
          status === "check" && !edited ? "border-accent" : "border-border hover:border-muted",
        ].join(" ")}
      />
      {cell.note && (
        <p id={`${id}-note`} className="text-xs text-muted">
          {cell.note}
        </p>
      )}
    </div>
  );
}

function ItemsEditor({
  invoice,
  edits,
  setEdit,
  disabled,
}: {
  invoice: Invoice;
  edits: Record<string, string>;
  setEdit: (path: string, value: string) => void;
  disabled: boolean;
}) {
  const items = invoice.items ?? [];
  if (items.length === 0) return <p className="py-6 text-center text-sm text-muted">No line items were found.</p>;
  return (
    <div className="-mx-5 overflow-x-auto">
      <table className="w-full min-w-max border-collapse text-sm">
        <caption className="sr-only">Line items (editable)</caption>
        <thead>
          <tr className="border-y border-border bg-surface-muted text-left text-xs font-medium uppercase tracking-wide text-muted">
            {ITEM_FIELDS.map((f) => (
              <th key={f.key} scope="col" className={`px-2 py-2 first:pl-5 last:pr-5 ${f.numeric ? "text-right" : ""}`}>
                {f.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {items.map((item, index) => (
            <tr key={index} className="border-b border-border last:border-b-0">
              {ITEM_FIELDS.map((f) => {
                const path = `items.${index}.${f.key}`;
                const cell = (item as Record<string, ExtractedField>)[f.key];
                const value = edits[path] ?? inputValue(cell);
                const flagged = cell?.status === "check" && edits[path] === undefined;
                return (
                  <td key={f.key} className="px-2 py-1.5 first:pl-5 last:pr-5">
                    <input
                      aria-label={`Line ${inputValue(item.line_number as ExtractedField) || index + 1} ${f.label}`}
                      title={flagged ? (cell?.note ?? "Check this value") : undefined}
                      value={value}
                      onChange={(event) => setEdit(path, event.target.value)}
                      disabled={disabled}
                      className={[
                        "min-h-8 rounded-md border bg-surface px-2 text-sm text-fg",
                        f.width,
                        f.numeric ? "text-right font-mono tabular-nums" : "",
                        flagged ? "border-accent bg-accent-soft" : "border-border",
                      ].join(" ")}
                    />
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function InvoiceReviewPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const detail = useApiQuery(
    () => unwrap(api.GET("/v1/dashboard/invoices/{invoice_id}", { params: { path: { invoice_id: id } } })),
    id,
  );
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<Action | null>(null);
  const [confirm, setConfirm] = useState<"reprocess" | "delete" | null>(null);
  const [message, setMessage] = useState<{ tone: "success" | "danger"; text: string } | null>(null);

  const record: InvoiceDetail | null = detail.data;
  const processing = record?.status === "processing";
  const { reload } = detail;

  // While the invoice is being read again, check back every few seconds.
  useEffect(() => {
    if (!processing) return;
    const timer = setInterval(reload, 5000);
    return () => clearInterval(timer);
  }, [processing, reload]);

  if (detail.status === "loading") return <Skeleton className="h-96" />;
  if (detail.status === "error") {
    return (
      <Alert>
        {detail.error instanceof ApiError && detail.error.status === 404 ? "This invoice does not exist or was deleted." : detail.error.message}{" "}
        <Link href="/invoices" className="underline">
          Back to invoices
        </Link>
      </Alert>
    );
  }

  const invoice = detail.data.extraction?.invoice ?? null;
  const validation = detail.data.extraction?.validation ?? null;
  const changes = invoice ? changedFields(edits, invoice) : {};
  const changeCount = Object.keys(changes).length;
  const locked = busy !== null || processing;

  function setEdit(path: string, value: string) {
    setEdits((current) => ({ ...current, [path]: value }));
  }

  async function run(action: Action) {
    setBusy(action);
    setMessage(null);
    const path = { params: { path: { invoice_id: id } } };
    try {
      if (action === "save") {
        await unwrap(api.PATCH("/v1/dashboard/invoices/{invoice_id}", { ...path, body: { fields: changes } }));
        setEdits({});
        setMessage({ tone: "success", text: `Saved ${changeCount} correction${changeCount === 1 ? "" : "s"}; the invoice was checked again.` });
      } else if (action === "approve") {
        await unwrap(api.POST("/v1/dashboard/invoices/{invoice_id}/approve", path));
        setMessage({ tone: "success", text: "Invoice approved." });
      } else if (action === "reprocess") {
        await unwrap(api.POST("/v1/dashboard/invoices/{invoice_id}/reprocess", path));
        setEdits({});
        setMessage({ tone: "success", text: "Reading the invoice again. This page updates when it is done." });
      } else {
        await unwrap(api.DELETE("/v1/dashboard/invoices/{invoice_id}", path));
        router.push("/invoices");
        return;
      }
      reload();
    } catch (error) {
      setMessage({ tone: "danger", text: error instanceof Error ? error.message : "Something went wrong." });
    } finally {
      setBusy(null);
      setConfirm(null);
    }
  }

  return (
    <>
      <Link href="/invoices" className="mb-2 inline-flex items-center gap-1 text-sm text-muted hover:text-fg">
        <ArrowLeft aria-hidden className="size-4" /> Invoices
      </Link>
      <PageHeader
        title={record?.invoice_number ?? record?.filename ?? "Invoice"}
        description={`${record?.filename} · ${record?.pages} page${record?.pages === 1 ? "" : "s"} · uploaded ${record ? formatDate(record.created_at) : ""}`}
      />

      <Card className="mb-6">
        <div className="flex flex-wrap items-center gap-3">
          {record && <ReviewStatusBadge status={record.status} />}
          <span className="text-sm text-fg">
            Confidence <Confidence value={record?.confidence} />
          </span>
          {validation && (
            <span className="text-sm text-fg">Validation {validation.status}</span>
          )}
          {record?.status === "approved" && record.approved_at && (
            <span className="text-xs text-muted">
              {record.approved_automatically ? "Approved automatically" : "Approved"} {formatDate(record.approved_at)}
            </span>
          )}
          <div className="ml-auto flex flex-wrap gap-2">
            <Button onClick={() => run("save")} disabled={changeCount === 0 || locked} loading={busy === "save"}>
              <Save aria-hidden className="size-4" />
              Save {changeCount > 0 ? `${changeCount} correction${changeCount === 1 ? "" : "s"}` : "corrections"}
            </Button>
            {record?.status !== "approved" && (
              <Button variant="secondary" onClick={() => run("approve")} disabled={locked || changeCount > 0 || !invoice} loading={busy === "approve"}>
                <CheckCheck aria-hidden className="size-4" />
                Approve
              </Button>
            )}
            <Button variant="secondary" onClick={() => setConfirm("reprocess")} disabled={locked || !record?.files_available}>
              <RefreshCw aria-hidden className="size-4" />
              Reprocess
            </Button>
            <Button variant="ghost" onClick={() => setConfirm("delete")} disabled={locked} aria-label="Delete invoice">
              <Trash2 aria-hidden className="size-4" />
            </Button>
          </div>
        </div>
        {changeCount > 0 && record?.status !== "approved" && (
          <p className="mt-2 text-xs text-muted">Save your corrections before approving.</p>
        )}
        {message && (
          <div className="mt-3">
            <Alert tone={message.tone}>{message.text}</Alert>
          </div>
        )}
        {processing && (
          <p className="mt-3 flex items-center gap-2 text-sm text-muted" role="status">
            <RefreshCw aria-hidden className="size-4 animate-spin" /> Reading the invoice again…
          </p>
        )}
        {record?.status === "failed" && record.error && (
          <div className="mt-3">
            <Alert>
              {record.error}. Reprocess to try again.
            </Alert>
          </div>
        )}
      </Card>

      <div className="grid gap-6 xl:grid-cols-[minmax(0,5fr)_minmax(0,6fr)] xl:items-start">
        <div className="xl:sticky xl:top-4">
          {record && !record.files_available ? (
            <Card title="Document">
              <p className="py-8 text-center text-sm text-muted">
                The uploaded file and page images were deleted by the retention policy. The extracted data is kept.
              </p>
            </Card>
          ) : (
            record && record.pages > 0 && <PageViewer invoiceId={id} pages={record.pages} />
          )}
        </div>

        <div className="flex flex-col gap-6">
          {!invoice ? (
            <Card>
              <p className="py-8 text-center text-sm text-muted">
                {processing ? "The fields appear when reading has finished." : "No fields were extracted. Reprocess the invoice."}
              </p>
            </Card>
          ) : (
            <>
              {detail.data.extraction?.warnings && detail.data.extraction.warnings.length > 0 && (
                <ul className="flex flex-col gap-1 rounded-lg bg-accent-soft p-3 text-sm text-fg">
                  {detail.data.extraction.warnings.map((warning) => (
                    <li key={warning} className="flex gap-2">
                      <AlertTriangle aria-hidden className="mt-0.5 size-4 shrink-0" />
                      {warning}
                    </li>
                  ))}
                </ul>
              )}
              {FIELD_GROUPS.map((group) => (
                <Card key={group.title} title={group.title}>
                  {group.fields.map(({ key, label }) => (
                    <FieldEditor
                      key={key}
                      label={label}
                      cell={field(invoice, key)}
                      value={edits[key] ?? inputValue(field(invoice, key))}
                      onChange={(value) => setEdit(key, value)}
                      disabled={locked}
                    />
                  ))}
                </Card>
              ))}
              <Card title={`Line items (${invoice.items?.length ?? 0})`} description="Highlighted cells need checking; hover for the reason.">
                <ItemsEditor invoice={invoice} edits={edits} setEdit={setEdit} disabled={locked} />
              </Card>
              {validation && <ChecksCard checks={validation.checks} status={validation.status} />}
            </>
          )}
          {record && record.corrections.length > 0 && (
            <Card title="Corrections" description="Every change made by reviewers, oldest first.">
              <ul className="flex flex-col">
                {record.corrections.map((c, index) => (
                  <li key={index} className="border-b border-border py-2 text-sm last:border-b-0">
                    <span className="font-mono text-xs text-muted">{c.field}</span>: {c.old_value ?? "—"} → <strong>{c.new_value ?? "—"}</strong>
                    <span className="block text-xs text-muted">
                      {c.corrected_by ?? "deleted user"} · {formatDate(c.created_at)}
                    </span>
                  </li>
                ))}
              </ul>
            </Card>
          )}
        </div>
      </div>

      <Dialog
        open={confirm !== null}
        title={confirm === "delete" ? "Delete this invoice?" : "Read this invoice again?"}
        description={
          confirm === "delete"
            ? "The record, its corrections, the uploaded file and page images are deleted permanently. Billing history is kept."
            : "OCR and extraction run again on the stored pages (a few minutes on a CPU). Current values, including unsaved and saved corrections, are replaced; the correction history is kept. Billed like an extraction."
        }
        onClose={() => setConfirm(null)}
        dismissible={busy === null}
      >
        <div className="flex justify-end gap-2">
          <Button variant="secondary" onClick={() => setConfirm(null)} disabled={busy !== null}>
            Cancel
          </Button>
          <Button
            variant={confirm === "delete" ? "danger" : "primary"}
            onClick={() => confirm && run(confirm)}
            loading={busy === confirm}
            data-autofocus
          >
            {confirm === "delete" ? "Delete" : "Reprocess"}
          </Button>
        </div>
      </Dialog>
    </>
  );
}
