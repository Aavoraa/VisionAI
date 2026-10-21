import { ApiError, type Schemas } from "@/lib/api/client";
import { ACCEPTED_IMAGE_TYPES, API_KEY_PATTERN } from "@/lib/playground";

export type InvoiceExtraction = Schemas["InvoiceExtraction"];
export type Invoice = Schemas["Invoice"];
export type LineItem = Schemas["LineItem"];
export type ValidationCheck = Schemas["ValidationCheck"];
export type FieldStatus = "ok" | "check" | "missing";
/** One extracted value (every invoice field has this shape). */
export type ExtractedField = {
  value?: unknown;
  status?: FieldStatus;
  source_text?: string | null;
  confidence?: number | null;
  note?: string | null;
};

export const ACCEPTED_DOCUMENT_TYPES = ["application/pdf", ...ACCEPTED_IMAGE_TYPES] as const;
export const MAX_DOCUMENT_BYTES = 20 * 1024 * 1024;

export type InvoiceFieldKey = Exclude<keyof Invoice, "items">;

/** Field groups as shown on the page, in reading order. */
export const FIELD_GROUPS: Array<{ title: string; fields: Array<{ key: InvoiceFieldKey; label: string; kind?: "money" | "date" }> }> = [
  {
    title: "Invoice details",
    fields: [
      { key: "invoice_number", label: "Invoice number" },
      { key: "invoice_date", label: "Invoice date", kind: "date" },
      { key: "place_of_supply", label: "Place of supply" },
      { key: "currency", label: "Currency" },
      { key: "delivery_note", label: "Delivery note" },
      { key: "payment_terms", label: "Payment terms" },
    ],
  },
  {
    title: "Seller",
    fields: [
      { key: "vendor_name", label: "Name" },
      { key: "vendor_address", label: "Address" },
      { key: "vendor_gstin", label: "GSTIN" },
    ],
  },
  {
    title: "Buyer",
    fields: [
      { key: "buyer_name", label: "Name" },
      { key: "buyer_address", label: "Address" },
      { key: "buyer_gstin", label: "GSTIN" },
    ],
  },
  {
    title: "Totals and taxes",
    fields: [
      { key: "total_quantity", label: "Total quantity" },
      { key: "subtotal", label: "Taxable value", kind: "money" },
      { key: "cgst", label: "CGST", kind: "money" },
      { key: "sgst", label: "SGST / UTGST", kind: "money" },
      { key: "igst", label: "IGST", kind: "money" },
      { key: "discount", label: "Discount", kind: "money" },
      { key: "round_off", label: "Round off", kind: "money" },
      { key: "total_amount", label: "Total amount", kind: "money" },
    ],
  },
];

export function validateInvoiceFile(file: Pick<File, "type" | "size">): string | null {
  if (!(ACCEPTED_DOCUMENT_TYPES as readonly string[]).includes(file.type)) return "Use a PDF or a JPEG, PNG, WebP or GIF image.";
  if (file.size > MAX_DOCUMENT_BYTES) return "Files can be at most 20 MB.";
  if (file.size === 0) return "This file is empty.";
  return null;
}

export function validateApiKey(key: string): string | null {
  const trimmed = key.trim();
  if (!trimmed) return "Paste an API key from the API keys page.";
  if (!API_KEY_PATTERN.test(trimmed)) return "API keys start with sk_live_ followed by the secret.";
  return null;
}

export type InvoiceAccepted = Schemas["InvoiceAccepted"];

/**
 * POST /v1/invoices/extract?mode=async with an API key (through the dashboard's /api rewrite):
 * stores the invoice and returns at once; reading continues in the background.
 */
export async function startInvoiceExtraction(
  input: { apiKey: string; file: File },
  options: { fetch?: typeof fetch; baseUrl?: string } = {},
): Promise<InvoiceAccepted> {
  const body = new FormData();
  body.append("file", input.file, input.file.name);
  const doFetch = options.fetch ?? fetch;
  let response: Response;
  try {
    response = await doFetch(`${options.baseUrl ?? "/api"}/v1/invoices/extract?mode=async`, {
      method: "POST",
      headers: { Authorization: `Bearer ${input.apiKey.trim()}` },
      body,
      credentials: "omit",
    });
  } catch {
    throw new ApiError(0, "network_error", "Could not reach the API. Is it running?");
  }
  if (!response.ok) throw await ApiError.fromResponse(response);
  return (await response.json()) as InvoiceAccepted;
}

const MONEY = new Intl.NumberFormat("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

/** Human-readable value: Indian digit grouping for money, "16 Jul 2026" for dates. */
export function displayValue(value: unknown, kind?: "money" | "date"): string {
  if (value === null || value === undefined || value === "") return "—";
  if (kind === "money") {
    const number = Number(value);
    return Number.isFinite(number) ? MONEY.format(number) : String(value);
  }
  if (kind === "date" && typeof value === "string" && /^\d{4}-\d{2}-\d{2}$/.test(value)) {
    return new Date(`${value}T00:00:00Z`).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });
  }
  return String(value);
}

export function field(invoice: Invoice, key: InvoiceFieldKey): ExtractedField {
  return (invoice[key] ?? {}) as ExtractedField;
}

/** Failed checks first, then passed, then skipped (what needs attention comes first). */
export function sortChecks(checks: ValidationCheck[]): ValidationCheck[] {
  const order = { fail: 0, pass: 1, skip: 2 } as const;
  return [...checks].sort((a, b) => order[a.status] - order[b.status]);
}

// ---------------------------------------------------------------------------
// Stored invoices (review)
// ---------------------------------------------------------------------------

export type ReviewStatus = Schemas["InvoiceSummary"]["status"];
export type InvoiceSummary = Schemas["InvoiceSummary"];
export type InvoiceDetail = Schemas["InvoiceDetail"];

export const REVIEW_TABS: Array<{ value: ReviewStatus | "all"; label: string }> = [
  { value: "needs_review", label: "Needs review" },
  { value: "reprocess", label: "Reprocess" },
  { value: "approved", label: "Approved" },
  { value: "all", label: "All" },
];

export function formatConfidence(value: number): string {
  return `${Math.round(value * 100)}%`;
}

/** A field's value as the text shown in an edit box ("" for no value). */
export function inputValue(cell: ExtractedField | undefined): string {
  const value = cell?.value;
  return value === null || value === undefined ? "" : String(value);
}

/**
 * The corrections to send: every edited box whose text differs from the stored value, keyed
 * by field path ("vendor_name", "items.3.rate").
 */
export function changedFields(edits: Record<string, string>, invoice: Invoice): Record<string, string | null> {
  const changes: Record<string, string | null> = {};
  for (const [path, text] of Object.entries(edits)) {
    const [head, index, name] = path.split(".");
    const cell =
      head === "items"
        ? ((invoice.items?.[Number(index)] as Record<string, ExtractedField> | undefined)?.[name ?? ""] as ExtractedField | undefined)
        : field(invoice, head as InvoiceFieldKey);
    if (text.trim() !== inputValue(cell).trim()) changes[path] = text.trim() === "" ? null : text;
  }
  return changes;
}
