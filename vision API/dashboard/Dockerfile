# syntax=docker/dockerfile:1
#
# Multi-stage build of the Next.js dashboard as a standalone Node server (~150 MB image,
# no node_modules at runtime, non-root).
#
#   docker build -t vision-dashboard \
#     --build-arg VISION_API_URL=http://api:8000 \
#     --build-arg NEXT_PUBLIC_API_URL=https://api.example.com .
#
# VISION_API_URL and NEXT_PUBLIC_API_URL are BUILD arguments: Next.js bakes next.config
# (the /api rewrite target) and NEXT_PUBLIC_* values into the build output.
# For reproducible builds, pin the base image by digest: NODE_IMAGE=node:24-alpine@sha256:<digest>

ARG NODE_IMAGE=node:24-alpine

# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------
FROM ${NODE_IMAGE} AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------
FROM ${NODE_IMAGE} AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
ARG VISION_API_URL=http://api:8000
ARG NEXT_PUBLIC_API_URL=http://localhost:8000
ENV VISION_API_URL=${VISION_API_URL} \
    NEXT_PUBLIC_API_URL=${NEXT_PUBLIC_API_URL} \
    NEXT_TELEMETRY_DISABLED=1
RUN npm run build

# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------
FROM ${NODE_IMAGE} AS runner
WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0

RUN addgroup --system --gid 10001 nextjs \
    && adduser --system --uid 10001 --ingroup nextjs --no-create-home nextjs

# Standalone server + the static assets it does not copy itself (see Next.js "output" docs).
# Files stay root-owned: the server only reads them.
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static
COPY --from=builder /app/public ./public

USER nextjs
EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["node", "-e", "fetch('http://127.0.0.1:3000/login').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"]

CMD ["node", "server.js"]
