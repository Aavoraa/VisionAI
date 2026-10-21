# Vision AI Console

Web dashboard for the Vision AI API: sign in, manage API keys, and follow usage and billing.
Built with Next.js 16 (App Router), React 19, Tailwind CSS 4 and a typed API client generated
from the backend's OpenAPI schema.

## How it talks to the API

The browser only ever talks to this app. `next.config.ts` forwards `/api/*` to the FastAPI
backend (`VISION_API_URL`), so:

- the API's session cookie (`vision_session`, httpOnly) is first-party, and no CORS is needed;
- the API remains the only place that checks permissions. `src/proxy.ts` just redirects
  visitors without a session cookie to `/login` (a fast pre-check, not a security boundary).

State-changing requests automatically carry the `X-CSRF-Token` header (read from the
`vision_csrf` cookie) via the client middleware in `src/lib/api/client.ts`.

## Getting started

Requires Node.js 20.9+ and a running API (see `../vision-api/README.md`).

```bash
npm install
cp .env.example .env.local      # Windows: copy .env.example .env.local
npm run dev                      # http://localhost:3000
```

Create an account at `/signup` (enabled outside production), or an administrator with
`python scripts/create_admin.py --email you@example.com` in `../vision-api`.

## Running with Docker

The whole platform (database, Redis, API, this console and its proxy) starts from
`../vision-api`:

```bash
cd ../vision-api
docker-compose up -d --build     # console: http://localhost:3000, API: http://localhost:8000
docker-compose run --rm api python scripts/create_admin.py --email you@example.com
```

The image (`Dockerfile`) is a multi-stage build of Next.js's standalone server: no
`node_modules` at runtime, non-root user, health check on `/login`. `VISION_API_URL` and
`NEXT_PUBLIC_API_URL` are **build arguments**, because Next.js bakes the `/api` rewrite target
and `NEXT_PUBLIC_*` values into the build; rebuild the image after changing them.

### Deploying behind a reverse proxy (required)

The API throttles sign-in attempts per client IP. Requests forwarded by this app reach the
API from the dashboard's address, and Next.js passes a browser-supplied `X-Forwarded-For`
through unchanged. So in any deployment:

1. put a reverse proxy in front of the console that **overwrites** `X-Forwarded-For` with
   the real client address (`proxy_set_header X-Forwarded-For $remote_addr;` in nginx, not
   `$proxy_add_x_forwarded_for`), and make it the only way to reach the console;
2. run the API with `--proxy-headers` and `FORWARDED_ALLOW_IPS=<dashboard address>` so it
   trusts that header from the console and from nobody else.

Docker Compose does exactly this (`../vision-api/deploy/nginx/dashboard.conf`), and
`../vision-api/tests/test_deploy.py` fails if that wiring is broken. Without it, every
visitor shares one sign-in throttle bucket (or can pick their own IP). Serve the console over
HTTPS in production; the API then marks its cookies `Secure`.

Pages send security headers (`X-Frame-Options: DENY`, `frame-ancestors 'none'`, `nosniff`,
a strict referrer policy and a restrictive `Permissions-Policy`), and `robots.txt` keeps the
console out of search engines. `/api/*` responses keep the API's own headers.

## Scripts

| Command | Purpose |
|---------|---------|
| `npm run dev` | Development server |
| `npm run build` / `npm start` | Production build and server |
| `npm test` | Unit and component tests (Vitest + Testing Library) |
| `npm run typecheck` | TypeScript, no emit |
| `npm run lint` | ESLint |
| `npm run gen:api` | Regenerate `src/lib/api/schema.d.ts` from `openapi.json` |

## Keeping API types in sync

`openapi.json` is exported from the backend code (no server needed), then turned into
TypeScript types:

```bash
# from ../vision-api
python scripts/export_openapi.py ../dashboard/openapi.json
# from here
npm run gen:api
```

Any API change that breaks the dashboard then shows up as a type error in `npm run typecheck`.

## Structure

```text
src/
├── proxy.ts                 # page guard: no session cookie -> /login?next=...
├── app/
│   ├── layout.tsx           # fonts (Fira Sans / Fira Code), metadata
│   ├── globals.css          # design tokens (light + dark), Tailwind theme
│   ├── contrast.test.ts     # WCAG AA check of every token pair, both themes
│   ├── (auth)/              # /login, /signup
│   └── (app)/               # signed-in area: /overview, /keys, /playground, /invoices, /usage, /billing, /settings
│       ├── invoices/        # list by review status; new/ = extract; [id]/ = review (image + editable fields)
│       └── admin/           # administrators: /admin/users, /admin/plans, /admin/usage
├── components/
│   ├── app-shell.tsx        # sidebar, mobile menu, account menu, loading/error states
│   ├── auth-form.tsx        # sign-in / sign-up form
│   ├── billing.tsx          # quota card (overview + billing)
│   ├── charts.tsx           # Recharts charts, each with a text summary and data table
│   ├── copy-button.tsx      # clipboard copy with feedback
│   ├── dialog.tsx           # accessible modal (focus in/out, trap, Escape)
│   ├── nav.ts               # navigation entries (admin-only items supported)
│   └── ui.tsx               # Button, Field, Card, StatCard, UsageBar, DataTable, Badge, Segmented, ...
└── lib/
    ├── api/client.ts        # typed client, CSRF middleware, ApiError, unwrap()
    ├── api/schema.d.ts      # generated from openapi.json (do not edit)
    ├── session.tsx          # current user; redirects to /login on 401/403
    ├── use-api.ts           # data loading with loading/error/refresh states
    ├── keys.ts              # key status and name rules
    ├── playground.ts        # Playground: form checks, POST /v1/vision with an API key
    ├── invoices.ts          # Invoices: extraction call, field groups, review helpers, formatting
    ├── plans.ts             # plan form: parsing, validation, create/update payloads
    ├── format.ts            # money (exact unit prices, chart ticks), numbers, UTC days, safe redirects
    └── validation.ts        # client-side form checks (the API re-validates)
```

## Design

Tokens live in `src/app/globals.css` and are exposed as Tailwind colours (`bg-surface`,
`text-muted`, `bg-primary`, ...), with a dark theme following the operating system.
`src/app/contrast.test.ts` checks every text/background pair the components use against
WCAG AA (4.5:1) in both themes, so a colour change that hurts legibility fails the tests.
Focus rings are always visible for keyboard users, dialogs keep and restore focus, status is
never shown by colour alone, every chart has a text summary and a data table, and animations
respect `prefers-reduced-motion`.

Recharts needs `react-is` matching the installed React major version; it is pinned as a
direct dependency for that reason.
