import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({ GET: vi.fn() }));
const router = vi.hoisted(() => ({ push: vi.fn() }));
vi.mock("@/lib/api/client", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/api/client")>();
  return { ...original, api };
});
vi.mock("next/navigation", () => ({ useRouter: () => router }));

import { apiResponse } from "@/test-utils";

import NewInvoicePage from "./page";

const KEY = "sk_live_TEST_fake_key_for_tests_000001";
const ID = "5b0e7c1a-9d2f-4e3b-8a6c-1f2e3d4c5b6a";
const PADDLE = { name: "PaddleOCR-VL-1.6-GGUF", invoice_extraction: true, tasks: [{ task: "OCR:", description: "Read all text" }] };
const fetchMock = vi.fn();

beforeEach(() => {
  api.GET.mockReset();
  api.GET.mockReturnValue(apiResponse(200, PADDLE));
  router.push.mockReset();
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
  URL.createObjectURL = vi.fn(() => "blob:preview");
  URL.revokeObjectURL = vi.fn();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

async function submit(user: ReturnType<typeof userEvent.setup>) {
  await user.type(await screen.findByLabelText("API key"), KEY);
  await user.upload(screen.getByLabelText(/Choose an invoice/), new File(["%PDF-1.7"], "invoice.pdf", { type: "application/pdf" }));
  await user.click(screen.getByRole("button", { name: "Extract fields" }));
}

describe("Extract an invoice", () => {
  it("explains when the serving model cannot read invoices", async () => {
    api.GET.mockReturnValue(apiResponse(200, { name: "mock-vision-1", invoice_extraction: false, tasks: null }));
    render(<NewInvoicePage />);
    expect(await screen.findByRole("alert")).toHaveTextContent("cannot read invoices");
    expect(screen.queryByRole("button", { name: "Extract fields" })).not.toBeInTheDocument();
  });

  it("validates before uploading", async () => {
    const user = userEvent.setup();
    render(<NewInvoicePage />);
    await user.click(await screen.findByRole("button", { name: "Extract fields" }));
    expect(screen.getByText("Paste an API key from the API keys page.")).toBeInTheDocument();
    expect(screen.getByText("Choose an invoice to read.")).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("shows PDFs without a preview and rejects other files", async () => {
    const user = userEvent.setup({ applyAccept: false });
    render(<NewInvoicePage />);
    await user.upload(await screen.findByLabelText(/Choose an invoice/), new File(["zip"], "a.zip", { type: "application/zip" }));
    expect(screen.getByText("Use a PDF or a JPEG, PNG, WebP or GIF image.")).toBeInTheDocument();
    await user.upload(screen.getByLabelText(/Choose an invoice/), new File(["%PDF"], "invoice.pdf", { type: "application/pdf" }));
    expect(screen.getByText("invoice.pdf")).toBeInTheDocument();
    expect(screen.queryByAltText("Selected invoice preview")).not.toBeInTheDocument();
  });

  it("uploads in the background and opens the review page", async () => {
    fetchMock.mockResolvedValue(Response.json({ id: ID, review_status: "processing", status_url: `/v1/invoices/${ID}` }, { status: 202 }));
    const user = userEvent.setup();
    render(<NewInvoicePage />);
    await submit(user);
    expect((fetchMock.mock.calls[0] as [string])[0]).toBe("/api/v1/invoices/extract?mode=async");
    expect(router.push).toHaveBeenCalledWith(`/invoices/${ID}`);
  });

  it("reports API errors with the request id", async () => {
    fetchMock.mockResolvedValue(
      Response.json({ error: { type: "authentication_error", message: "Invalid API key", param: null }, request_id: "req-7" }, { status: 401 }),
    );
    const user = userEvent.setup();
    render(<NewInvoicePage />);
    await submit(user);
    expect(await screen.findByRole("alert")).toHaveTextContent("Invalid API key");
    expect(screen.getByText("Request ID: req-7")).toBeInTheDocument();
    expect(screen.getByText("This key is invalid, revoked or expired.")).toBeInTheDocument();
    expect(router.push).not.toHaveBeenCalled();
  });
});
