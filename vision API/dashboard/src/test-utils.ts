import type { Schemas } from "@/lib/api/client";

// Shared helpers for component tests that mock the API client.

/** Mimic an openapi-fetch result for a given status and JSON body. */
export function apiResponse(status: number, body: unknown = null) {
  const response = new Response(body === null ? null : JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
  return Promise.resolve(response.ok ? { data: body ?? undefined, response } : { error: body, response });
}

export function apiErrorResponse(status: number, message: string, param: string | null = null, type = "invalid_request_error") {
  return apiResponse(status, { error: { type, message, param }, request_id: "req-test" });
}

export const USAGE_REPORT = {
  start: "2026-09-22",
  end: "2026-09-24",
  days: 3,
  totals: {
    requests: 8,
    successful: 6,
    failed: 2,
    input_tokens: 2000,
    output_tokens: 300,
    images: 9,
    avg_processing_ms: 812.4,
    cost_usd: "0.019500",
  },
  daily: [
    { date: "2026-09-22", requests: 5, failed: 1, input_tokens: 1200, output_tokens: 200, images: 5, cost_usd: "0.011000" },
    { date: "2026-09-23", requests: 0, failed: 0, input_tokens: 0, output_tokens: 0, images: 0, cost_usd: "0.000000" },
    { date: "2026-09-24", requests: 3, failed: 1, input_tokens: 800, output_tokens: 100, images: 4, cost_usd: "0.008500" },
  ],
  by_model: [{ model: "mock-vision-1", requests: 8, input_tokens: 2000, output_tokens: 300 }],
  by_key: [
    { api_key_id: "k1", name: "production", prefix: "sk_live_AbCd", requests: 6, input_tokens: 1500, output_tokens: 250 },
    { api_key_id: "k2", name: "laptop", prefix: "sk_live_EfGh", requests: 2, input_tokens: 500, output_tokens: 50 },
  ],
};

export const BILLING = {
  plan: {
    id: "p1",
    name: "Default",
    is_default: true,
    price_per_1k_input_tokens_usd: "0.000500",
    price_per_1k_output_tokens_usd: "0.001500",
    price_per_image_usd: "0.002000",
    monthly_request_limit: 5000,
    monthly_spend_limit_usd: null,
  },
  period: { start: "2026-09-01", end: "2026-09-30", resets_at: "2026-10-01T00:00:00Z" },
  usage: { requests: 4500, input_tokens: 10, output_tokens: 5, images: 3 },
  cost: { total_usd: "3.357765", projected_usd: "4.197206" },
  quota: {
    request_limit: 5000,
    requests_used: 4500,
    requests_remaining: 500,
    spend_limit_usd: null,
    spend_used_usd: "3.357765",
    spend_remaining_usd: null,
  },
  history: [
    { month: "2026-08", requests: 3120, cost_usd: "8.101200" },
    { month: "2026-09", requests: 4500, cost_usd: "3.357765" },
  ],
};

// Invoice extraction (made-up values in the shape of POST /v1/invoices/extract).
export const extracted = (value: unknown) => ({ value, status: "ok" as const, source_text: null, confidence: null, note: null });
const flagged = (value: unknown, note = "verify") => ({ value, status: "check" as const, source_text: null, confidence: null, note });

export const INVOICE = {
  invoice_number: extracted("CR/134/26-27"),
  invoice_date: extracted("2026-07-16"),
  buyer_gstin: extracted("33AADCE6591N1Z7"),
  place_of_supply: flagged("Tamil Nadu (33)", "no 'Place of Supply' printed; taken from the buyer's state"),
  total_amount: extracted("49969.00"),
  items: [
    { line_number: extracted(35), description: extracted("4285-40 RMB BLOSSOM ELB"), quantity: extracted("1"), taxable_value: extracted("513.00") },
    { line_number: extracted(36), description: flagged("3352-32 COPPER JAKKAD ELB", "rows misaligned"), quantity: extracted("1"), taxable_value: extracted("288.00") },
  ],
} as unknown as Schemas["Invoice"];

export const RESULT = {
  invoice: INVOICE,
  model: "PaddleOCR-VL-1.6-GGUF",
  reasoning_model: "Qwen3-VL-2B-Instruct",
  pages: 1,
  raw_text: "--- header OCR ---",
  warnings: ["1 field(s) read differently by Qwen3-VL-2B-Instruct; marked 'check'"],
  validation: {
    status: "failed",
    checks: [
      { id: "grand_total", name: "Grand total", status: "pass", message: "47,589.30 + taxes 2,379.66 + round-off 0.04 = 49,969.00", fields: [] },
      { id: "items_sum", name: "Items add up", status: "skip", message: "The document starts at line 35: earlier pages are not included", fields: [] },
      { id: "line_arithmetic", name: "Line amounts", status: "fail", message: "line 36: 1 × 320.00 − 10% = 288.00, read 388.00", fields: [] },
    ],
  },
  usage: { input_tokens: 4210, output_tokens: 1980 },
  processing_ms: 142000,
} as unknown as Schemas["InvoiceExtraction"];


export const INVOICE_SUMMARY = {
  id: "5b0e7c1a-9d2f-4e3b-8a6c-1f2e3d4c5b6a",
  filename: "aug.pdf",
  status: "needs_review",
  confidence: 0.75,
  invoice_number: "CR/134/26-27",
  vendor_name: "SHALIHA RETAIL VENTURES A/C - (26-27)",
  total_amount: "49969.00",
  pages: 1,
  error: null,
  created_at: "2026-09-26T10:15:00Z",
  updated_at: "2026-09-26T10:17:22Z",
  approved_at: null,
  approved_automatically: false,
} as unknown as Schemas["InvoiceSummary"];

export const INVOICE_DETAIL = {
  ...INVOICE_SUMMARY,
  content_type: "application/pdf",
  files_available: true,
  extraction: { ...RESULT, id: INVOICE_SUMMARY.id, review_status: "needs_review", confidence: 0.75 },
  corrections: [
    {
      field: "vendor_name", old_value: "KETAIL VENTURES", new_value: "SHALIHA RETAIL VENTURES A/C - (26-27)",
      old_status: "check", corrected_by: "ada@example.com", created_at: "2026-09-26T10:20:05Z",
    },
  ],
} as unknown as Schemas["InvoiceDetail"];
