import type { Plan, Schemas } from "@/lib/api/client";

/** Form state: everything is a string, exactly as typed (prices are sent as exact decimals). */
export type PlanForm = {
  name: string;
  input: string;
  output: string;
  image: string;
  requestLimit: string; // empty = unlimited
  spendLimit: string; // empty = no cap
  isDefault: boolean;
};

export type PlanFormErrors = Partial<Record<keyof PlanForm, string>>;

const USD = /^\d{1,7}(\.\d{1,6})?$/;
const INTEGER = /^\d{1,10}$/;

export const EMPTY_PLAN_FORM: PlanForm = {
  name: "",
  input: "",
  output: "",
  image: "",
  requestLimit: "",
  spendLimit: "",
  isDefault: false,
};

/** Strip trailing zeros from the API's 6-decimal strings for editing ("0.000500" -> "0.0005"). */
function trimUsd(value: string | null): string {
  if (value === null) return "";
  return value.includes(".") ? value.replace(/0+$/, "").replace(/\.$/, "") : value;
}

export function formFromPlan(plan: Plan): PlanForm {
  return {
    name: plan.name,
    input: trimUsd(plan.price_per_1k_input_tokens_usd),
    output: trimUsd(plan.price_per_1k_output_tokens_usd),
    image: trimUsd(plan.price_per_image_usd),
    requestLimit: plan.monthly_request_limit === null ? "" : String(plan.monthly_request_limit),
    spendLimit: trimUsd(plan.monthly_spend_limit_usd),
    isDefault: plan.is_default,
  };
}

export function validatePlanForm(form: PlanForm): PlanFormErrors {
  const errors: PlanFormErrors = {};
  const name = form.name.trim();
  if (!name) errors.name = "Give the plan a name.";
  else if (name.length > 50) errors.name = "Use at most 50 characters.";
  for (const field of ["input", "output", "image"] as const) {
    const value = form[field].trim();
    if (!value) errors[field] = "Required. Use 0 for free.";
    else if (!USD.test(value)) errors[field] = "A non-negative amount with at most 6 decimals, e.g. 0.0005.";
  }
  if (form.requestLimit.trim() && !INTEGER.test(form.requestLimit.trim())) {
    errors.requestLimit = "A whole number, or leave empty for unlimited.";
  }
  if (form.spendLimit.trim() && !USD.test(form.spendLimit.trim())) {
    errors.spendLimit = "An amount with at most 6 decimals, or leave empty for no cap.";
  }
  return errors;
}

export function createPayload(form: PlanForm): Schemas["PlanCreate"] {
  return {
    name: form.name.trim(),
    price_per_1k_input_tokens_usd: form.input.trim(),
    price_per_1k_output_tokens_usd: form.output.trim(),
    price_per_image_usd: form.image.trim(),
    monthly_request_limit: form.requestLimit.trim() ? Number(form.requestLimit.trim()) : null,
    monthly_spend_limit_usd: form.spendLimit.trim() || null,
    is_default: form.isDefault,
  };
}

/** Only the fields that changed, so an edit never rewrites values nobody touched. */
export function updatePayload(original: PlanForm, form: PlanForm): Schemas["PlanUpdate"] {
  const next = createPayload(form);
  const before = createPayload(original);
  const changes: Schemas["PlanUpdate"] = {};
  if (next.name !== before.name) changes.name = next.name;
  if (Number(next.price_per_1k_input_tokens_usd) !== Number(before.price_per_1k_input_tokens_usd)) {
    changes.price_per_1k_input_tokens_usd = next.price_per_1k_input_tokens_usd;
  }
  if (Number(next.price_per_1k_output_tokens_usd) !== Number(before.price_per_1k_output_tokens_usd)) {
    changes.price_per_1k_output_tokens_usd = next.price_per_1k_output_tokens_usd;
  }
  if (Number(next.price_per_image_usd) !== Number(before.price_per_image_usd)) {
    changes.price_per_image_usd = next.price_per_image_usd;
  }
  if (next.monthly_request_limit !== before.monthly_request_limit) {
    changes.monthly_request_limit = next.monthly_request_limit;
  }
  const spendBefore = before.monthly_spend_limit_usd === null ? null : Number(before.monthly_spend_limit_usd);
  const spendNext = next.monthly_spend_limit_usd === null ? null : Number(next.monthly_spend_limit_usd);
  if (spendNext !== spendBefore) changes.monthly_spend_limit_usd = next.monthly_spend_limit_usd;
  if (form.isDefault && !original.isDefault) changes.is_default = true;
  return changes;
}
