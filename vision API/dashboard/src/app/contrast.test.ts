// @vitest-environment node
// WCAG AA contrast for every text/background pair the components actually use, in both
// themes, computed from the real tokens in globals.css (so changing a colour cannot
// silently break legibility).
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const css = readFileSync(join(__dirname, "globals.css"), "utf8");

function tokens(block: string): Record<string, string> {
  return Object.fromEntries([...block.matchAll(/--([a-z-]+):\s*(#[0-9a-f]{6})/gi)].map((m) => [m[1], m[2]]));
}

const light = tokens(css.slice(css.indexOf(":root {"), css.indexOf("@media (prefers-color-scheme: dark)")));
const dark = { ...light, ...tokens(css.slice(css.indexOf("@media (prefers-color-scheme: dark)"), css.indexOf("@theme"))) };

function luminance(hex: string): number {
  const [r, g, b] = [1, 3, 5].map((i) => {
    const c = parseInt(hex.slice(i, i + 2), 16) / 255;
    return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

export function contrast(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

// [text token, background token, where it is used]
const PAIRS: Array<[string, string, string]> = [
  ["fg", "bg", "page text"],
  ["fg", "surface", "card text"],
  ["muted", "surface", "secondary text on cards"],
  ["muted", "bg", "secondary text on the page"],
  ["muted", "surface-muted", "table headers"],
  ["fg", "surface-muted", "code blocks, secret display"],
  ["primary", "surface", "links"],
  ["primary", "primary-soft", "active navigation item"],
  ["primary-fg", "primary", "primary buttons, selected range"],
  ["danger-fg", "danger", "destructive buttons"],
  ["danger", "surface", "error text"],
  ["danger", "danger-soft", "error alerts, revoked badges"],
  ["success", "success-soft", "success alerts, active badges"],
  ["fg", "accent-soft", "warnings, expired badges"],
];

describe.each([
  ["light", light],
  ["dark", dark],
])("%s theme", (_name, theme) => {
  it.each(PAIRS)("%s on %s (%s) meets WCAG AA 4.5:1", (text, background) => {
    expect(theme[text], `missing token --${text}`).toBeDefined();
    expect(theme[background], `missing token --${background}`).toBeDefined();
    expect(contrast(theme[text], theme[background])).toBeGreaterThanOrEqual(4.5);
  });
});

it("computes known ratios", () => {
  expect(contrast("#000000", "#ffffff")).toBeCloseTo(21, 5);
  expect(contrast("#ffffff", "#ffffff")).toBeCloseTo(1, 5);
});
