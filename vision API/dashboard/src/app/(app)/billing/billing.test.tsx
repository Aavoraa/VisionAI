import { render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({ GET: vi.fn() }));
vi.mock("@/lib/api/client", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/api/client")>();
  return { ...original, api };
});

import { BILLING, apiResponse } from "@/test-utils";

import BillingPage from "./page";

beforeEach(() => api.GET.mockReset());

describe("BillingPage", () => {
  it("shows the plan's exact unit prices and limits", async () => {
    api.GET.mockReturnValue(apiResponse(200, BILLING));
    render(<BillingPage />);
    const plan = (await screen.findByText("Default plan")).closest("section")!;

    expect(within(plan).getByText("$0.0005 per 1K")).toBeInTheDocument();
    expect(within(plan).getByText("$0.0015 per 1K")).toBeInTheDocument();
    expect(within(plan).getByText("$0.002 each")).toBeInTheDocument();
    expect(within(plan).getByText("5,000")).toBeInTheDocument();
    expect(within(plan).getByText("None")).toBeInTheDocument(); // no spend cap
  });

  it("shows this month's charges and quota", async () => {
    api.GET.mockReturnValue(apiResponse(200, BILLING));
    render(<BillingPage />);
    await screen.findByText("Default plan");

    expect(screen.getByRole("group", { name: "Charges this month" })).toHaveTextContent("$3.36");
    expect(screen.getByRole("group", { name: "Projected month-end" })).toHaveTextContent("$4.20");
    const requests = screen.getByRole("progressbar", { name: "Requests" });
    expect(requests).toHaveAttribute("aria-valuenow", "90");
    expect(screen.getByText(/nearly used/)).toBeInTheDocument();
    expect(screen.getByText("Spend: no monthly cap on this plan.")).toBeInTheDocument();
  });

  it("warns when the allowance is used up", async () => {
    api.GET.mockReturnValue(
      apiResponse(200, { ...BILLING, quota: { ...BILLING.quota, requests_used: 5000, requests_remaining: 0 } }),
    );
    render(<BillingPage />);
    expect(await screen.findByText(/New API requests are refused \(HTTP 402\) until Oct 1, 2026/)).toBeInTheDocument();
  });

  it("charts the last months with a text summary", async () => {
    api.GET.mockReturnValue(apiResponse(200, BILLING));
    render(<BillingPage />);
    expect(
      await screen.findByRole("img", { name: "Charges for the last 2 months: Aug 2026 $8.10, Sep 2026 $3.36." }),
    ).toBeInTheDocument();
  });

  it("reports load failures", async () => {
    api.GET.mockReturnValue(apiResponse(502));
    render(<BillingPage />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not load billing");
  });
});
