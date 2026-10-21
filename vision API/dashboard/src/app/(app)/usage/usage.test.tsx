import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({ GET: vi.fn() }));
vi.mock("@/lib/api/client", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/api/client")>();
  return { ...original, api };
});

import { toDailyPoints } from "@/components/charts";
import { USAGE_REPORT, apiResponse } from "@/test-utils";

import UsagePage from "./page";

beforeEach(() => api.GET.mockReset());

function statValue(label: string): string {
  return screen.getByRole("group", { name: label }).querySelector("p.font-mono")!.textContent!;
}

describe("UsagePage", () => {
  it("summarises the period", async () => {
    api.GET.mockReturnValue(apiResponse(200, USAGE_REPORT));
    render(<UsagePage />);
    await screen.findByText("By model");

    expect(api.GET).toHaveBeenCalledWith("/v1/dashboard/usage", { params: { query: { days: 30 } } });
    expect(statValue("Requests")).toBe("8");
    expect(statValue("Success rate")).toBe("75.0%");
    expect(statValue("Tokens")).toBe("2,300");
    expect(statValue("Spend")).toBe("$0.02");
    expect(screen.getByText("Avg. 812 ms per request")).toBeInTheDocument();
    expect(screen.getByText(/Sep 22, 2026 to Sep 24, 2026 \(UTC\)/)).toBeInTheDocument();
  });

  it("switches the time range", async () => {
    api.GET.mockReturnValue(apiResponse(200, USAGE_REPORT));
    const user = userEvent.setup();
    render(<UsagePage />);
    await screen.findByText("By model");

    const range = screen.getByRole("group", { name: "Time range" });
    expect(within(range).getByRole("button", { name: "30 days" })).toHaveAttribute("aria-pressed", "true");
    await user.click(within(range).getByRole("button", { name: "7 days" }));

    await waitFor(() =>
      expect(api.GET).toHaveBeenLastCalledWith("/v1/dashboard/usage", { params: { query: { days: 7 } } }),
    );
    expect(within(range).getByRole("button", { name: "7 days" })).toHaveAttribute("aria-pressed", "true");
  });

  it("breaks usage down by model and key", async () => {
    api.GET.mockReturnValue(apiResponse(200, USAGE_REPORT));
    render(<UsagePage />);
    const byKey = await screen.findByRole("table", { name: "Usage by API key" });
    const keyRows = within(byKey).getAllByRole("row").slice(1);
    expect(keyRows.map((row) => within(row).getAllByRole("cell")[0].textContent)).toEqual([
      "productionsk_live_AbCd…",
      "laptopsk_live_EfGh…",
    ]);
    expect(within(screen.getByRole("table", { name: "Usage by model" })).getByText("mock-vision-1")).toBeInTheDocument();
  });

  it("describes charts in text and offers the numbers as a table", async () => {
    api.GET.mockReturnValue(apiResponse(200, USAGE_REPORT));
    const user = userEvent.setup();
    render(<UsagePage />);
    expect(await screen.findByRole("img", { name: "Requests per day: 8 in total, 2 failed." })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Spend per day, $0.02 in total." })).toBeInTheDocument();

    const [toggle] = screen.getAllByRole("button", { name: "Show data table" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    const table = screen.getAllByRole("table", { name: "Requests and spend per day" })[0];
    const firstRow = within(table).getAllByRole("row")[1];
    expect(within(firstRow).getAllByRole("cell").map((c) => c.textContent)).toEqual(["Sep 22, 2026", "4", "1", "$0.01"]);
  });

  it("handles an empty period", async () => {
    api.GET.mockReturnValue(
      apiResponse(200, {
        ...USAGE_REPORT,
        totals: { ...USAGE_REPORT.totals, requests: 0, successful: 0, failed: 0, avg_processing_ms: null },
        by_model: [],
        by_key: [],
      }),
    );
    render(<UsagePage />);
    expect(await screen.findAllByText("No requests in this period.")).toHaveLength(2);
    expect(statValue("Success rate")).toBe("—");
    expect(screen.getByText("No requests yet")).toBeInTheDocument();
  });
});

describe("toDailyPoints", () => {
  it("splits successful and failed requests and parses costs", () => {
    expect(toDailyPoints(USAGE_REPORT)[0]).toEqual({ date: "2026-09-22", successful: 4, failed: 1, cost: 0.011 });
  });
});
