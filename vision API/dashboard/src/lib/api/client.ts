import createClient, { type Middleware } from "openapi-fetch";

import type { components, paths } from "./schema";

export type Schemas = components["schemas"];
export type User = Schemas["UserOut"];
export type AuthResponse = Schemas["AuthResponse"];
export type BillingOverview = Schemas["BillingOverview"];
export type ApiKey = Schemas["ApiKeyOut"];
export type ApiKeyCreated = Schemas["ApiKeyCreated"];
export type UsageReport = Schemas["UsageReport"];
export type PlatformUsageReport = Schemas["PlatformUsageReport"];
export type AdminUser = Schemas["AdminUserOut"];
export type Plan = Schemas["PlanWithUsers"];

export const CSRF_COOKIE = "vision_csrf";
const UNSAFE_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

/** Error returned by the API: `{"error": {"type", "message", "param"}, "request_id"}`. */
export class ApiError extends Error {
  readonly status: number;
  readonly type: string;
  readonly param: string | null;
  readonly requestId: string | null;

  constructor(status: number, type: string, message: string, param: string | null = null, requestId: string | null = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.type = type;
    this.param = param;
    this.requestId = requestId;
  }

  static async fromResponse(response: Response, body?: unknown): Promise<ApiError> {
    const payload = body ?? (await response.clone().json().catch(() => undefined));
    const error = (payload as { error?: { type?: string; message?: string; param?: string | null } } | undefined)?.error;
    const requestId =
      (payload as { request_id?: string | null } | undefined)?.request_id ?? response.headers.get("x-request-id");
    if (error?.message) {
      return new ApiError(response.status, error.type ?? "api_error", error.message, error.param ?? null, requestId);
    }
    // Proxy/network-level failures (e.g. the API is down) do not use the API's error shape.
    const message = response.status >= 500 ? "The service is unavailable. Please try again." : "Request failed.";
    return new ApiError(response.status, "api_error", message, null, requestId);
  }
}

export function readCookie(name: string): string | null {
  if (typeof document === "undefined") return null;
  for (const part of document.cookie.split(";")) {
    const [key, ...rest] = part.trim().split("=");
    if (key === name) return decodeURIComponent(rest.join("="));
  }
  return null;
}

/** Adds the session's CSRF token to state-changing requests (the API rejects them without it). */
export const csrfMiddleware: Middleware = {
  onRequest({ request }) {
    if (UNSAFE_METHODS.has(request.method.toUpperCase())) {
      const token = readCookie(CSRF_COOKIE);
      if (token) request.headers.set("X-CSRF-Token", token);
    }
    return request;
  },
};

export function createApiClient(options: { baseUrl?: string; fetch?: typeof fetch } = {}) {
  const client = createClient<paths>({
    baseUrl: options.baseUrl ?? "/api",
    fetch: options.fetch,
    credentials: "same-origin",
  });
  client.use(csrfMiddleware);
  return client;
}

export type ApiClient = ReturnType<typeof createApiClient>;

/** Browser-side client: requests go to this origin's `/api`, proxied to the FastAPI backend. */
export const api = createApiClient();

/** Return `data`, or throw an `ApiError` carrying the API's message. */
export async function unwrap<T>(result: Promise<{ data?: T; error?: unknown; response: Response }>): Promise<T> {
  let settled: { data?: T; error?: unknown; response: Response };
  try {
    settled = await result;
  } catch {
    throw new ApiError(0, "network_error", "Cannot reach the server. Check your connection and try again.");
  }
  const { data, error, response } = settled;
  if (!response.ok) throw await ApiError.fromResponse(response, error);
  return data as T;
}
