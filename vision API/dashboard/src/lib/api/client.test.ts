import { describe, expect, it, vi } from "vitest";

import { ApiError, createApiClient, readCookie, unwrap } from "./client";

function jsonResponse(status: number, body: unknown, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", ...headers },
  });
}

function clientReturning(response: () => Response) {
  const requests: Request[] = [];
  const fetchMock = vi.fn(async (input: Request) => {
    requests.push(input);
    return response();
  });
  const client = createApiClient({ baseUrl: "http://test.local/api", fetch: fetchMock as unknown as typeof fetch });
  return { client, requests };
}

describe("CSRF header", () => {
  it("is added to state-changing requests from the csrf cookie", async () => {
    document.cookie = "vision_csrf=token-123; path=/";
    const { client, requests } = clientReturning(() => new Response(null, { status: 204 }));

    await client.POST("/v1/auth/logout");
    await client.DELETE("/v1/dashboard/keys/{key_id}", { params: { path: { key_id: "k" } } });

    expect(requests.map((r) => r.headers.get("X-CSRF-Token"))).toEqual(["token-123", "token-123"]);
  });

  it("is not added to safe requests", async () => {
    document.cookie = "vision_csrf=token-123; path=/";
    const { client, requests } = clientReturning(() => jsonResponse(200, {}));
    await client.GET("/v1/auth/me");
    expect(requests[0].headers.get("X-CSRF-Token")).toBeNull();
  });

  it("is omitted when there is no csrf cookie", async () => {
    const { client, requests } = clientReturning(() => new Response(null, { status: 204 }));
    await client.POST("/v1/auth/logout");
    expect(requests[0].headers.has("X-CSRF-Token")).toBe(false);
  });

  it("requests go to the proxied API path", async () => {
    const { client, requests } = clientReturning(() => jsonResponse(200, {}));
    await client.GET("/v1/dashboard/billing");
    expect(requests[0].url).toBe("http://test.local/api/v1/dashboard/billing");
  });
});

describe("readCookie", () => {
  it("decodes values and ignores other cookies", () => {
    document.cookie = "other=1; path=/";
    document.cookie = "vision_csrf=a%2Fb%3Dc; path=/";
    expect(readCookie("vision_csrf")).toBe("a/b=c");
    expect(readCookie("missing")).toBeNull();
  });
});

describe("unwrap", () => {
  it("returns data for successful responses", async () => {
    const { client } = clientReturning(() => jsonResponse(200, { email: "ada@example.com" }));
    await expect(unwrap(client.GET("/v1/auth/me"))).resolves.toEqual({ email: "ada@example.com" });
  });

  it("throws an ApiError carrying the API's error fields", async () => {
    const { client } = clientReturning(() =>
      jsonResponse(
        422,
        { error: { type: "invalid_request_error", message: "password: too short", param: "password" }, request_id: "req-1" },
      ),
    );
    const error = await unwrap(
      client.POST("/v1/auth/signup", { body: { email: "a@b.co", password: "x" } }),
    ).catch((e: unknown) => e);

    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
      status: 422,
      type: "invalid_request_error",
      message: "password: too short",
      param: "password",
      requestId: "req-1",
    });
  });

  it("uses a generic message when a proxy error has no API error body", async () => {
    const { client } = clientReturning(() => new Response("Bad Gateway", { status: 502, headers: { "x-request-id": "r2" } }));
    const error = (await unwrap(client.GET("/v1/auth/me")).catch((e: unknown) => e)) as ApiError;
    expect(error.status).toBe(502);
    expect(error.message).toBe("The service is unavailable. Please try again.");
  });

  it("reports network failures clearly", async () => {
    const client = createApiClient({
      baseUrl: "http://test.local/api",
      fetch: (async () => {
        throw new TypeError("fetch failed");
      }) as unknown as typeof fetch,
    });
    const error = (await unwrap(client.GET("/v1/auth/me")).catch((e: unknown) => e)) as ApiError;
    expect(error.type).toBe("network_error");
    expect(error.message).toMatch(/Cannot reach the server/);
  });
});
