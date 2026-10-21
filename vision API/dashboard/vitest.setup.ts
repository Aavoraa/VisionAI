import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// jsdom has no ResizeObserver; Recharts' ResponsiveContainer needs one (charts then render
// at 0x0, which is fine: tests assert on the accessible summary and data tables instead).
if (typeof window !== "undefined" && !("ResizeObserver" in window)) {
  class ResizeObserverStub {
    observe(): void {}
    unobserve(): void {}
    disconnect(): void {}
  }
  Object.defineProperty(window, "ResizeObserver", { value: ResizeObserverStub, writable: true });
}

afterEach(() => {
  // Node-environment tests (e.g. the proxy) have no DOM.
  if (typeof document === "undefined") return;
  cleanup();
  document.cookie.split(";").forEach((cookie) => {
    const name = cookie.split("=")[0]?.trim();
    if (name) document.cookie = `${name}=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/`;
  });
});
