import { describe, expect, it, vi } from "vitest";

import { ApiError } from "@/lib/api/client";
import { INVOICE, extracted as ok } from "@/test-utils";

import {
  changedFields,
  displayValue,
  startInvoiceExtraction,
  formatConfidence,
  inputValue,
  validateApiKey,
  validateInvoiceFile,
} from "./invoices";

const KEY = "sk_live_TEST_fake_key_for_tests_000001";
describe("validation", () => {
  it("accepts PDFs and images up to 20 MB", () => {
    expect(validateInvoiceFile({ type: "application/pdf", size: 20 * 1024 * 1024 })).toBeNull();
    expect(validateInvoiceFile({ type: "image/jpeg", size: 1 })).toBeNull();
    expect(validateInvoiceFile({ type: "application/zip", size: 1 })).toMatch(/PDF or a JPEG/);
    expect(validateInvoiceFile({ type: "application/pdf", size: 20 * 1024 * 1024 + 1 })).toMatch(/20 MB/);
    expect(validateInvoiceFile({ type: "application/pdf", size: 0 })).toMatch(/empty/);
  });

  it("checks the API key shape", () => {
    expect(validateApiKey(` ${KEY} `)).toBeNull();
    expect(validateApiKey("")).toMatch(/Paste an API key/);
    expect(validateApiKey("Bearer abc")).toMatch(/sk_live_/);
  });
});

it("formats values for reading", () => {
  expect(displayValue("49969.00", "money")).toBe("49,969.00");
  expect(displayValue("117589.3", "money")).toBe("1,17,589.30"); // Indian digit grouping
  expect(displayValue("2026-07-16", "date")).toBe("16 Jul 2026");
  expect(displayValue(null)).toBe("—");
  expect(displayValue("CR/134/26-27")).toBe("CR/134/26-27");
});

describe("startInvoiceExtraction", () => {
  it("uploads the file in background mode with the key and no cookies", async () => {
    const accepted = { id: "5b0e7c1a-9d2f-4e3b-8a6c-1f2e3d4c5b6a", review_status: "processing", status_url: "/v1/invoices/5b0e7c1a-9d2f-4e3b-8a6c-1f2e3d4c5b6a" };
    const fetchMock = vi.fn().mockResolvedValue(Response.json(accepted, { status: 202 }));
    const file = new File([new Uint8Array([37, 80, 68, 70])], "invoice.pdf", { type: "application/pdf" });
    expect(await startInvoiceExtraction({ apiKey: ` ${KEY} `, file }, { fetch: fetchMock })).toEqual(accepted);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/v1/invoices/extract?mode=async");
    expect(init.headers).toEqual({ Authorization: `Bearer ${KEY}` });
    expect(init.credentials).toBe("omit");
    expect(((init.body as FormData).get("file") as File).name).toBe("invoice.pdf");
  });

  it("reports API errors and an unreachable API", async () => {
    const failing = vi.fn().mockResolvedValue(
      Response.json({ error: { type: "service_unavailable", message: "Invoice extraction needs PaddleOCR-VL", param: null } }, { status: 503 }),
    );
    const file = new File(["x"], "a.png", { type: "image/png" });
    await expect(startInvoiceExtraction({ apiKey: KEY, file }, { fetch: failing })).rejects.toMatchObject({ status: 503 });
    const down = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
    const error = await startInvoiceExtraction({ apiKey: KEY, file }, { fetch: down }).catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).message).toMatch(/Could not reach the API/);
  });
});

describe("review helpers", () => {
  it("formats confidence and edit values", () => {
    expect(formatConfidence(0.754)).toBe("75%");
    expect(inputValue(ok("49969.00"))).toBe("49969.00");
    expect(inputValue(ok(null))).toBe("");
    expect(inputValue(undefined)).toBe("");
  });

  it("sends only the boxes that really changed", () => {
    expect(
      changedFields(
        {
          invoice_number: "CR/134/26-27", // unchanged
          total_amount: " 49,970.00 ",
          buyer_gstin: "", // cleared
          "items.1.quantity": "2",
          "items.0.quantity": "1 ", // unchanged apart from spaces
        },
        INVOICE,
      ),
    ).toEqual({ total_amount: " 49,970.00 ", buyer_gstin: null, "items.1.quantity": "2" });
  });
});
