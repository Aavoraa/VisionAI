import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({ GET: vi.fn(), PATCH: vi.fn(), POST: vi.fn(), DELETE: vi.fn() }));
const router = vi.hoisted(() => ({ push: vi.fn(), replace: vi.fn() }));
vi.mock("@/lib/api/client", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/api/client")>();
  return { ...original, api };
});
vi.mock("next/navigation", () => ({
  useParams: () => ({ id: "5b0e7c1a-9d2f-4e3b-8a6c-1f2e3d4c5b6a" }),
  useRouter: () => router,
}));

import { INVOICE_DETAIL, apiResponse } from "@/test-utils";

import InvoiceReviewPage from "./page";

const ID = "5b0e7c1a-9d2f-4e3b-8a6c-1f2e3d4c5b6a";
const PATH = { params: { path: { invoice_id: ID } } };

beforeEach(() => {
  for (const mock of [api.GET, api.PATCH, api.POST, api.DELETE, router.push]) mock.mockReset();
  api.GET.mockReturnValue(apiResponse(200, INVOICE_DETAIL));
});

describe("Invoice review", () => {
  it("shows the document next to editable fields", async () => {
    render(<InvoiceReviewPage />);
    expect(await screen.findByRole("heading", { name: "CR/134/26-27" })).toBeInTheDocument();
    expect(screen.getByAltText("Invoice page 1")).toHaveAttribute("src", `/api/v1/dashboard/invoices/${ID}/pages/1`);
    expect(screen.getByLabelText("Invoice number")).toHaveValue("CR/134/26-27");
    expect(screen.getByLabelText("Place of supply")).toHaveValue("Tamil Nadu (33)");
    expect(screen.getByText("no 'Place of Supply' printed; taken from the buyer's state")).toBeInTheDocument();
    expect(screen.getByLabelText("Line 36 Description")).toHaveAttribute("title", "rows misaligned");
    expect(screen.getByText("Needs review")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Corrections" })).toBeInTheDocument();
    expect(screen.getByText(/ada@example.com/)).toBeInTheDocument();
  });

  it("saves only the changed values, then reloads", async () => {
    api.PATCH.mockReturnValue(apiResponse(200, INVOICE_DETAIL));
    const user = userEvent.setup();
    render(<InvoiceReviewPage />);
    const save = await screen.findByRole("button", { name: "Save corrections" });
    expect(save).toBeDisabled();

    const total = screen.getByLabelText("Total amount");
    await user.clear(total);
    await user.type(total, "49970.00");
    await user.clear(screen.getByLabelText("Line 36 Amount"));
    await user.type(screen.getByLabelText("Line 36 Amount"), "288.00");
    expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled(); // save first
    await user.click(screen.getByRole("button", { name: "Save 1 correction" }));

    expect(api.PATCH).toHaveBeenCalledWith("/v1/dashboard/invoices/{invoice_id}", { ...PATH, body: { fields: { total_amount: "49970.00" } } });
    expect(await screen.findByText("Saved 1 correction; the invoice was checked again.")).toBeInTheDocument();
    expect(api.GET).toHaveBeenCalledTimes(2);
  });

  it("approves", async () => {
    api.POST.mockReturnValue(apiResponse(200, { ...INVOICE_DETAIL, status: "approved" }));
    const user = userEvent.setup();
    render(<InvoiceReviewPage />);
    await user.click(await screen.findByRole("button", { name: "Approve" }));
    expect(api.POST).toHaveBeenCalledWith("/v1/dashboard/invoices/{invoice_id}/approve", PATH);
    expect(await screen.findByText("Invoice approved.")).toBeInTheDocument();
  });

  it("asks before deleting, then goes back to the list", async () => {
    api.DELETE.mockReturnValue(Promise.resolve({ response: new Response(null, { status: 204 }) }));
    const user = userEvent.setup();
    render(<InvoiceReviewPage />);
    await user.click(await screen.findByRole("button", { name: "Delete invoice" }));
    const dialog = screen.getByRole("dialog", { name: "Delete this invoice?" });
    await user.click(within(dialog).getByRole("button", { name: "Delete" }));
    expect(api.DELETE).toHaveBeenCalledWith("/v1/dashboard/invoices/{invoice_id}", PATH);
    expect(router.push).toHaveBeenCalledWith("/invoices");
  });

  it("shows reprocessing progress", async () => {
    api.GET.mockReturnValue(apiResponse(200, { ...INVOICE_DETAIL, status: "processing", confidence: null, extraction: null }));
    render(<InvoiceReviewPage />);
    expect(await screen.findByText(/Reading the invoice again/)).toBeInTheDocument();
    expect(screen.getByText("The fields appear when reading has finished.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reprocess" })).toBeDisabled();
  });

  it("explains when retention removed the document", async () => {
    api.GET.mockReturnValue(apiResponse(200, { ...INVOICE_DETAIL, files_available: false }));
    render(<InvoiceReviewPage />);
    expect(await screen.findByText(/deleted by the retention policy/)).toBeInTheDocument();
    expect(screen.queryByAltText("Invoice page 1")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reprocess" })).toBeDisabled();
  });

  it("explains a missing invoice", async () => {
    api.GET.mockReturnValue(apiResponse(404, { error: { type: "not_found_error", message: "Invoice not found", param: null } }));
    render(<InvoiceReviewPage />);
    expect(await screen.findByText(/does not exist or was deleted/)).toBeInTheDocument();
  });
});
