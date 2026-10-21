"use client";

import { Plus, RefreshCw } from "lucide-react";
import { useState, type FormEvent } from "react";

import { Dialog } from "@/components/dialog";
import { Alert, Badge, Button, Card, DataTable, Field, PageHeader, Skeleton, type Column } from "@/components/ui";
import { ApiError, api, unwrap, type Plan } from "@/lib/api/client";
import { formatNumber, formatUnitPrice, formatUsd } from "@/lib/format";
import {
  EMPTY_PLAN_FORM,
  createPayload,
  formFromPlan,
  updatePayload,
  validatePlanForm,
  type PlanForm,
  type PlanFormErrors,
} from "@/lib/plans";
import { useApiQuery } from "@/lib/use-api";

// Map API field names back to form fields so server-side validation errors land on the input.
const API_FIELD: Record<string, keyof PlanForm> = {
  name: "name",
  price_per_1k_input_tokens_usd: "input",
  price_per_1k_output_tokens_usd: "output",
  price_per_image_usd: "image",
  monthly_request_limit: "requestLimit",
  monthly_spend_limit_usd: "spendLimit",
};

function PlanDialog({ plan, open, onClose, onSaved }: { plan: Plan | null; open: boolean; onClose: () => void; onSaved: () => void }) {
  const original = plan ? formFromPlan(plan) : EMPTY_PLAN_FORM;
  const [form, setForm] = useState<PlanForm>(original);
  const [errors, setErrors] = useState<PlanFormErrors>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const set = (field: keyof PlanForm) => (event: { target: { value: string } }) =>
    setForm((current) => ({ ...current, [field]: event.target.value }));

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setFormError(null);
    const found = validatePlanForm(form);
    setErrors(found);
    if (Object.keys(found).length > 0) return;

    setSaving(true);
    try {
      if (plan) {
        const changes = updatePayload(original, form);
        if (Object.keys(changes).length > 0) {
          await unwrap(api.PATCH("/v1/admin/plans/{plan_id}", { params: { path: { plan_id: plan.id } }, body: changes }));
        }
      } else {
        await unwrap(api.POST("/v1/admin/plans", { body: createPayload(form) }));
      }
      onSaved();
    } catch (error) {
      const field = error instanceof ApiError && error.param ? API_FIELD[error.param] : undefined;
      if (field) setErrors({ [field]: (error as ApiError).message });
      else setFormError(error instanceof Error ? error.message : "Could not save the plan.");
      setSaving(false);
    }
  }

  return (
    <Dialog open={open} title={plan ? `Edit ${plan.name}` : "Create plan"} onClose={onClose} dismissible={!saving} size="lg">
      <form onSubmit={onSubmit} noValidate className="flex flex-col gap-4">
        {formError && <Alert>{formError}</Alert>}
        <Field label="Name" value={form.name} onChange={set("name")} error={errors.name} maxLength={50} data-autofocus />
        <fieldset className="grid gap-3 sm:grid-cols-3 sm:items-end">
          <legend className="mb-1.5 text-sm font-medium text-fg">Prices (USD)</legend>
          <Field label="Per 1K input tokens" inputMode="decimal" value={form.input} onChange={set("input")} error={errors.input} />
          <Field label="Per 1K output tokens" inputMode="decimal" value={form.output} onChange={set("output")} error={errors.output} />
          <Field label="Per image" inputMode="decimal" value={form.image} onChange={set("image")} error={errors.image} />
        </fieldset>
        <div className="grid gap-3 sm:grid-cols-2 sm:items-start">
          <Field
            label="Monthly request limit"
            inputMode="numeric"
            value={form.requestLimit}
            onChange={set("requestLimit")}
            hint="Empty = unlimited"
            error={errors.requestLimit}
          />
          <Field
            label="Monthly spend cap (USD)"
            inputMode="decimal"
            value={form.spendLimit}
            onChange={set("spendLimit")}
            hint="Empty = no cap"
            error={errors.spendLimit}
          />
        </div>
        <label className="flex cursor-pointer items-start gap-2 text-sm text-fg">
          <input
            type="checkbox"
            checked={form.isDefault}
            disabled={original.isDefault}
            onChange={(event) => setForm((current) => ({ ...current, isDefault: event.target.checked }))}
            className="mt-0.5 size-4 cursor-pointer accent-[var(--primary)]"
          />
          <span>
            Default plan
            <span className="block text-xs text-muted">
              {original.isDefault
                ? "This is the default. To change it, make another plan the default."
                : "Applies to every user without an assigned plan."}
            </span>
          </span>
        </label>
        <p className="text-xs text-muted">Price changes apply to new requests only; past charges keep their price.</p>
        <div className="flex justify-end gap-2">
          <Button variant="secondary" onClick={onClose} disabled={saving}>
            Cancel
          </Button>
          <Button type="submit" loading={saving}>
            {plan ? "Save changes" : "Create plan"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

export default function AdminPlansPage() {
  const plans = useApiQuery(() => unwrap(api.GET("/v1/admin/plans")));
  const [editing, setEditing] = useState<Plan | "new" | null>(null);

  const columns: Column<Plan>[] = [
    {
      header: "Plan",
      cell: (plan) => (
        <div className="flex items-center gap-2">
          <span className="font-medium">{plan.name}</span>
          {plan.is_default && <Badge tone="success">Default</Badge>}
        </div>
      ),
    },
    { header: "1K input", cell: (plan) => formatUnitPrice(plan.price_per_1k_input_tokens_usd), align: "right", hideBelow: "md" },
    { header: "1K output", cell: (plan) => formatUnitPrice(plan.price_per_1k_output_tokens_usd), align: "right", hideBelow: "md" },
    { header: "Image", cell: (plan) => formatUnitPrice(plan.price_per_image_usd), align: "right", hideBelow: "md" },
    {
      header: "Requests / month",
      cell: (plan) => (plan.monthly_request_limit === null ? "Unlimited" : formatNumber(plan.monthly_request_limit)),
      align: "right",
      hideBelow: "xl",
    },
    {
      header: "Spend cap",
      cell: (plan) => (plan.monthly_spend_limit_usd === null ? "None" : formatUsd(plan.monthly_spend_limit_usd)),
      align: "right",
      hideBelow: "xl",
    },
    { header: "Users", cell: (plan) => formatNumber(plan.users), align: "right" },
    {
      header: "Actions",
      align: "right",
      cell: (plan) => (
        <Button variant="ghost" className="px-3 font-sans" aria-label={`Edit ${plan.name}`} onClick={() => setEditing(plan)}>
          Edit
        </Button>
      ),
    },
  ];

  return (
    <>
      <PageHeader
        title="Plans"
        description="Prices and monthly limits. Users without an assigned plan are on the default plan."
        action={
          <Button onClick={() => setEditing("new")}>
            <Plus aria-hidden className="size-4" />
            Create plan
          </Button>
        }
      />
      {plans.status === "loading" && <Skeleton className="h-48" />}
      {plans.status === "error" && (
        <div className="flex flex-col items-start gap-3">
          <Alert>Could not load plans: {plans.error.message}</Alert>
          <Button variant="secondary" onClick={plans.reload}>
            <RefreshCw aria-hidden className="size-4" />
            Try again
          </Button>
        </div>
      )}
      {plans.status === "success" && (
        <Card title="All plans" description="Users counts include users who follow the default plan.">
          <DataTable caption="Plans" columns={columns} rows={plans.data.items} rowKey={(plan) => plan.id} />
        </Card>
      )}
      <PlanDialog
        key={editing === null ? "closed" : editing === "new" ? "new" : editing.id}
        open={editing !== null}
        plan={editing === "new" ? null : editing}
        onClose={() => setEditing(null)}
        onSaved={() => {
          setEditing(null);
          plans.reload();
        }}
      />
    </>
  );
}
