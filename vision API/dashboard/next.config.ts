import type { NextConfig } from "next";

// FastAPI backend. The browser only ever talks to this app's origin: `/api/*` is proxied
// to the API, so session cookies stay first-party (httpOnly, SameSite=Lax) and no CORS
// configuration is needed.
//
// Read at BUILD time: next.config is serialised into the production server, so container
// images take VISION_API_URL as a build argument (see Dockerfile).
const apiUrl = (process.env.VISION_API_URL ?? "http://localhost:8000").replace(/\/+$/, "");

const DEFAULT_PROXY_TIMEOUT_MS = 330_000;
const configuredTimeout = Number(process.env.VISION_PROXY_TIMEOUT_MS);
const proxyTimeoutMs =
  Number.isInteger(configuredTimeout) && configuredTimeout > 0 ? configuredTimeout : DEFAULT_PROXY_TIMEOUT_MS;

// Applied to the dashboard's own pages. API responses (/api/*) keep the API's stricter headers.
const securityHeaders = [
  { key: "X-Frame-Options", value: "DENY" },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=(), payment=()" },
  // Script/style sources are left to Next.js defaults (its inline bootstrap would need nonces);
  // these directives still block framing, plugins, base-tag hijacking and off-site form posts.
  { key: "Content-Security-Policy", value: "frame-ancestors 'none'; object-src 'none'; base-uri 'self'; form-action 'self'" },
];

const nextConfig: NextConfig = {
  output: "standalone",
  poweredByHeader: false,
  experimental: {
    // Rewrites give up after 30 s by default; image analysis on a CPU can take minutes. Keep
    // this above the API's own inference deadline (VISION_MODEL_TIMEOUT_SECONDS, default 120 s)
    // so the API's 504 timeout_error reaches the browser instead of a bare proxy error; raise
    // both together (e.g. whole-page OCR on a laptop CPU). Read at build/dev-server start.
    proxyTimeout: proxyTimeoutMs,
  },
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${apiUrl}/:path*` }];
  },
  async headers() {
    return [{ source: "/((?!api/).*)", headers: securityHeaders }];
  },
};

export default nextConfig;
