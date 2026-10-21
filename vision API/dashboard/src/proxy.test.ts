// @vitest-environment node
import { NextRequest } from "next/server";
import { describe, expect, it } from "vitest";

import { isPublicPath, proxy } from "./proxy";

function request(path: string, sessionCookie?: string): NextRequest {
  const headers = sessionCookie ? { cookie: `vision_session=${sessionCookie}` } : undefined;
  return new NextRequest(new URL(path, "http://console.local"), { headers });
}

describe("proxy page guard", () => {
  it("redirects anonymous visitors to sign-in, remembering where they were going", () => {
    const response = proxy(request("/settings?tab=security"));
    expect(response.status).toBe(307);
    const location = new URL(response.headers.get("location")!);
    expect(location.pathname).toBe("/login");
    expect(location.searchParams.get("next")).toBe("/settings?tab=security");
  });

  it("does not add ?next for the root page", () => {
    const location = new URL(proxy(request("/")).headers.get("location")!);
    expect(location.pathname).toBe("/login");
    expect(location.search).toBe("");
  });

  it("lets requests with a session cookie through (the API validates it)", () => {
    const response = proxy(request("/overview", "some-token"));
    expect(response.headers.get("location")).toBeNull();
    expect(response.headers.get("x-middleware-next")).toBe("1");
  });

  it.each(["/login", "/signup", "/login?next=/x"])("never redirects public page %s", (path) => {
    expect(proxy(request(path)).headers.get("location")).toBeNull();
    expect(proxy(request(path, "stale-token")).headers.get("location")).toBeNull(); // no redirect loops
  });

  it("classifies public paths exactly", () => {
    expect(isPublicPath("/login")).toBe(true);
    expect(isPublicPath("/signup/")).toBe(true);
    expect(isPublicPath("/loginx")).toBe(false);
    expect(isPublicPath("/overview")).toBe(false);
  });
});
