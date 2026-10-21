"use client";

import { AlertTriangle, CheckCircle2, CircleDashed, CircleSlash, Clock, RefreshCw, XCircle } from "lucide-react";

import { Badge, Card } from "@/components/ui";
import { formatConfidence, sortChecks, type FieldStatus, type ReviewStatus, type ValidationCheck } from "@/lib/invoices";

const STATUS: Record<FieldStatus, { label: string; tone: "success" | "warning" | "neutral"; Icon: typeof CheckCircle2 }> = {
  ok: { label: "OK", tone: "success", Icon: CheckCircle2 },
  check: { label: "Check", tone: "warning", Icon: AlertTriangle },
  missing: { label: "Missing", tone: "neutral", Icon: CircleDashed },
};

export function StatusBadge({ status }: { status: FieldStatus }) {
  const { label, tone, Icon } = STATUS[status];
  return (
    <Badge tone={tone}>
      <Icon aria-hidden className="mr-1 size-3" />
      {label}
    </Badge>
  );
}

const REVIEW_STATUS: Record<ReviewStatus, { label: string; tone: "success" | "warning" | "neutral" | "danger"; Icon: typeof CheckCircle2 }> = {
  approved: { label: "Approved", tone: "success", Icon: CheckCircle2 },
  needs_review: { label: "Needs review", tone: "warning", Icon: AlertTriangle },
  reprocess: { label: "Reprocess", tone: "danger", Icon: RefreshCw },
  processing: { label: "Processing", tone: "neutral", Icon: Clock },
  failed: { label: "Failed", tone: "danger", Icon: XCircle },
};

export function ReviewStatusBadge({ status }: { status: ReviewStatus }) {
  const { label, tone, Icon } = REVIEW_STATUS[status];
  return (
    <Badge tone={tone}>
      <Icon aria-hidden className="mr-1 size-3" />
      {label}
    </Badge>
  );
}

export function reviewStatusLabel(status: ReviewStatus): string {
  return REVIEW_STATUS[status].label;
}

/** "92%" with a text colour that follows the routing bands (not colour alone: the number is shown). */
export function Confidence({ value }: { value: number | null | undefined }) {
  if (value === null || value === undefined) return <span className="text-muted">—</span>;
  const tone = value >= 0.9 ? "text-success" : value >= 0.7 ? "text-fg" : "text-danger";
  return <span className={`font-mono tabular-nums ${tone}`}>{formatConfidence(value)}</span>;
}

const CHECK_STATUS = {
  pass: { label: "Passed", Icon: CheckCircle2, className: "text-success" },
  fail: { label: "Failed", Icon: XCircle, className: "text-danger" },
  skip: { label: "Skipped", Icon: CircleSlash, className: "text-muted" },
} as const;

export function ChecksCard({ checks, status }: { checks: ValidationCheck[]; status: "passed" | "failed" }) {
  const failed = checks.filter((c) => c.status === "fail").length;
  const skipped = checks.filter((c) => c.status === "skip").length;
  return (
    <Card
      title="Checks"
      description={
        status === "passed"
          ? `All ${checks.length - skipped} checks that could run passed${skipped ? ` (${skipped} skipped)` : ""}.`
          : `${failed} check${failed === 1 ? "" : "s"} failed: the values involved are marked Check.`
      }
    >
      <ul className="flex flex-col">
        {sortChecks(checks).map((check) => {
          const { label, Icon, className } = CHECK_STATUS[check.status];
          return (
            <li key={check.id} className="flex gap-3 border-b border-border py-2.5 last:border-b-0">
              <Icon aria-label={label} className={`mt-0.5 size-4 shrink-0 ${className}`} />
              <div className="min-w-0">
                <p className="text-sm font-medium text-fg">{check.name}</p>
                <p className="break-words text-xs text-muted">{check.message}</p>
              </div>
            </li>
          );
        })}
      </ul>
    </Card>
  );
}

