import { describe, expect, it } from "vitest";

import {
  formatCompact,
  formatDate,
  formatDay,
  formatMonth,
  formatNumber,
  formatPercent,
  formatUnitPrice,
  formatUsd,
  formatUsdTick,
  safeNextPath,
  successRate,
  usageRatio,
} from "./format";
import { validateCredentials, validatePasswordChange } from "./validation";

describe("formatUsd", () => {
  it.each([
    ["3.357765", "$3.36"],
    ["25.000000", "$25.00"],
    ["0.000000", "$0.00"],
    ["0.002144", "$0.0021"],
    ["0.000500", "$0.0005"],
    ["1234.5", "$1,234.50"],
    [null, "—"],
    ["not a number", "—"],
  ])("%s -> %s", (input, expected) => {
    expect(formatUsd(input)).toBe(expected);
  });
});

describe("numbers", () => {
  it("formats counts", () => {
    expect(formatNumber(1234567)).toBe("1,234,567");
    expect(formatCompact(9999)).toBe("9,999");
    expect(formatCompact(412300)).toBe("412.3K");
  });

  it("computes usage ratios, clamped, with null for unlimited", () => {
    expect(usageRatio(50, 200)).toBe(0.25);
    expect(usageRatio(300, 200)).toBe(1);
    expect(usageRatio(5, 0)).toBe(1);
    expect(usageRatio(5, null)).toBeNull();
  });

  it("formats dates and rejects garbage", () => {
    expect(formatDate("2026-10-01T00:00:00Z", { dateStyle: "medium", timeZone: "UTC" })).toBe("Oct 1, 2026");
    expect(formatDate("nope")).toBe("—");
  });
});

describe("days, months, prices, rates", () => {
  it("formats API calendar days in UTC (no off-by-one west of UTC)", () => {
    expect(formatDay("2026-09-01")).toBe("Sep 1");
    expect(formatDay("2026-09-01", { dateStyle: "long" })).toBe("September 1, 2026");
    expect(formatMonth("2026-12")).toBe("Dec 2026");
    expect(formatDay("garbage")).toBe("—");
  });

  it("shows unit prices exactly", () => {
    expect(formatUnitPrice("0.000500")).toBe("$0.0005");
    expect(formatUnitPrice("0.002000")).toBe("$0.002");
    expect(formatUnitPrice("25.000000")).toBe("$25.00");
    expect(formatUnitPrice("0.000001")).toBe("$0.000001");
  });

  it("keeps chart-axis money ticks distinct", () => {
    const ticks = [0, 0.004, 0.008, 0.012, 0.016].map(formatUsdTick);
    expect(ticks).toEqual(["$0", "$0.004", "$0.008", "$0.012", "$0.016"]);
    expect(new Set(ticks).size).toBe(ticks.length);
    expect(formatUsdTick(12.5)).toBe("$12.5");
    expect(formatUsdTick(1234)).toBe("$1,230");
  });

  it("computes and formats success rates", () => {
    expect(successRate({ requests: 0, successful: 0 })).toBeNull();
    expect(formatPercent(successRate({ requests: 8, successful: 6 }))).toBe("75.0%");
    expect(formatPercent(1)).toBe("100%");
    expect(formatPercent(null)).toBe("—");
  });
});

describe("safeNextPath (open-redirect protection)", () => {
  it.each([
    ["/settings", "/settings"],
    ["/overview?tab=1", "/overview?tab=1"],
    [null, "/overview"],
    ["", "/overview"],
    ["https://evil.example.com", "/overview"],
    ["//evil.example.com", "/overview"],
    ["/\\evil.example.com", "/overview"],
    ["javascript:alert(1)", "/overview"],
    ["/login", "/overview"],
  ])("%s -> %s", (input, expected) => {
    expect(safeNextPath(input)).toBe(expected);
  });
});

describe("validation", () => {
  it("checks sign-in and sign-up credentials", () => {
    expect(validateCredentials("login", "ada@example.com", "x")).toEqual({});
    expect(validateCredentials("signup", "ada@example.com", "short")).toEqual({ password: "Use at least 10 characters." });
    expect(validateCredentials("login", "not-an-email", "")).toEqual({
      email: "Enter a valid email address.",
      password: "Enter your password.",
    });
  });

  it("checks password changes", () => {
    expect(validatePasswordChange("old password!", "a new passphrase", "a new passphrase")).toEqual({});
    expect(validatePasswordChange("", "short", "other")).toEqual({
      current: "Enter your current password.",
      next: "Use at least 10 characters.",
      confirm: "The passwords do not match.",
    });
    expect(validatePasswordChange("same password", "same password", "same password").next).toMatch(/different/);
  });
});
