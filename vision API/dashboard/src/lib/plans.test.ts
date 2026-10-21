import { describe, expect, it } from "vitest";

import { EMPTY_PLAN_FORM, createPayload, formFromPlan, updatePayload, validatePlanForm, type PlanForm } from "./plans";

const PLAN = {
  id: "p1",
  name: "Default",
  is_default: true,
  price_per_1k_input_tokens_usd: "0.000500",
  price_per_1k_output_tokens_usd: "0.001500",
  price_per_image_usd: "0.002000",
  monthly_request_limit: 5000,
  monthly_spend_limit_usd: "25.000000",
  users: 3,
};

const VALID: PlanForm = { ...EMPTY_PLAN_FORM, name: "Pro", input: "0.0004", output: "0.0012", image: "0.0015" };

describe("plan form", () => {
  it("round-trips API values into editable strings", () => {
    expect(formFromPlan(PLAN)).toEqual({
      name: "Default",
      input: "0.0005",
      output: "0.0015",
      image: "0.002",
      requestLimit: "5000",
      spendLimit: "25",
      isDefault: true,
    });
    expect(formFromPlan({ ...PLAN, monthly_request_limit: null, monthly_spend_limit_usd: null })).toMatchObject({
      requestLimit: "",
      spendLimit: "",
    });
  });

  it("validates prices and limits", () => {
    expect(validatePlanForm(VALID)).toEqual({});
    expect(validatePlanForm(EMPTY_PLAN_FORM)).toEqual({
      name: "Give the plan a name.",
      input: "Required. Use 0 for free.",
      output: "Required. Use 0 for free.",
      image: "Required. Use 0 for free.",
    });
    const bad = validatePlanForm({ ...VALID, input: "-1", output: "0.0000001", image: "1e3", requestLimit: "2.5", spendLimit: "abc" });
    expect(Object.keys(bad).sort()).toEqual(["image", "input", "output", "requestLimit", "spendLimit"]);
  });

  it("sends prices as exact decimal strings and empty limits as null", () => {
    expect(createPayload(VALID)).toEqual({
      name: "Pro",
      price_per_1k_input_tokens_usd: "0.0004",
      price_per_1k_output_tokens_usd: "0.0012",
      price_per_image_usd: "0.0015",
      monthly_request_limit: null,
      monthly_spend_limit_usd: null,
      is_default: false,
    });
  });

  it("updates only what changed", () => {
    const original = formFromPlan(PLAN);
    expect(updatePayload(original, original)).toEqual({});
    expect(updatePayload(original, { ...original, image: "0.0025" })).toEqual({ price_per_image_usd: "0.0025" });
    expect(updatePayload(original, { ...original, input: "0.000500" })).toEqual({}); // same value, different spelling
    expect(updatePayload(original, { ...original, requestLimit: "", spendLimit: "" })).toEqual({
      monthly_request_limit: null,
      monthly_spend_limit_usd: null,
    });
    const nonDefault = { ...original, isDefault: false };
    expect(updatePayload(nonDefault, { ...nonDefault, isDefault: true })).toEqual({ is_default: true });
  });
});
