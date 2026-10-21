import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({ GET: vi.fn() }));
vi.mock("@/lib/api/client", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/api/client")>();
  return { ...original, api };
});

import { INVOICE_SUMMARY, apiResponse } from "@/test-utils";

import InvoiceListPage from "./page";

const COUNTS = { processing: 1, needs_review: 1, approved: 4, reprocess: 0, failed: 0 };

beforeEach(() => {
  api.GET.mockReset();
  api.GET.mockReturnValue(apiResponse(200, { items: [INVOICE_SUMMARY], total: 1, counts: COUNTS, limit: 25, offset: 0 }));
});

describe("Invoices list", () => {
  it("opens on the review queue with counts per status", async () => {
    render(<InvoiceListPage />);
    const link = await screen.findByRole("link", { name: "CR/134/26-27" });
    expect(link).toHaveAttribute("href", `/invoices/${INVOICE_SUMMARY.id}`);
    expect(api.GET).toHaveBeenCalledWith("/v1/dashboard/invoices", { params: { query: { status: "needs_review", limit: 25, offset: 0 } } });
    expect(screen.getByRole("button", { name: "Needs review (1)" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "Approved (4)" })).toBeInTheDocument();
    expect(screen.getByText("1 processing · 0 failed (under All)")).toBeInTheDocument();

    const row = within(screen.getByRole("table", { name: "Invoices" })).getAllByRole("row")[1]!;
    expect(row).toHaveTextContent("49,969.00");
    expect(row).toHaveTextContent("75%");
    expect(row).toHaveTextContent("Needs review");
    expect(screen.getByRole("link", { name: /Extract an invoice/ })).toHaveAttribute("href", "/invoices/new");
    expect(screen.getByRole("link", { name: "Invoices CSV" })).toHaveAttribute("href", "/api/v1/dashboard/invoices/export?format=csv&status=needs_review");
    expect(screen.getByRole("link", { name: "Line items CSV" })).toHaveAttribute("download");
  });

  it("switches tabs", async () => {
    const user = userEvent.setup();
    render(<InvoiceListPage />);
    await user.click(await screen.findByRole("button", { name: "All" }));
    expect(api.GET).toHaveBeenLastCalledWith("/v1/dashboard/invoices", { params: { query: { status: undefined, limit: 25, offset: 0 } } });
    expect(screen.getByRole("link", { name: "JSON" })).toHaveAttribute("href", "/api/v1/dashboard/invoices/export?format=json");
  });

  it("says when the queue is empty", async () => {
    api.GET.mockReturnValue(apiResponse(200, { items: [], total: 0, counts: { ...COUNTS, needs_review: 0 }, limit: 25, offset: 0 }));
    render(<InvoiceListPage />);
    expect(await screen.findByText("Nothing waiting for review.")).toBeInTheDocument();
  });
});
