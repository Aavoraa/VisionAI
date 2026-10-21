# Vision AI API Platform

A secure, rate-limited HTTP API for analysing images with open-weight vision-language models
(VLMs). Send one or more images (file upload or URL) with a prompt; get back the model's
answer, token usage and a request ID, synchronously or as an async job you poll.

- **API-key authentication**: `sk_live_…` keys, stored only as SHA-256 hashes
- **Hardened image intake**: content-based type detection, decompression-bomb limits, EXIF/GPS stripping, downscaling
- **SSRF-safe URL fetching**: DNS pinning, private/metadata IP blocking, re-validated redirects, size and time caps
- **Sync or async** requests (`mode=async` → `202` + `GET /v1/jobs/{job_id}`)
- **Per-user rate limiting** (Redis), **usage accounting** and **billing**: priced plans with monthly request/spend quotas (PostgreSQL)
- **Dashboard API**: email/password accounts with roles, key management, usage and billing views, admin tools
- **Web console** (`../dashboard`, Next.js): API keys, usage charts, billing and quotas for customers; users, plans and platform usage for administrators
- **Pluggable model backends**: an instant mock (default, no downloads) or Hugging Face `transformers` on CPU/GPU
- **OpenAPI docs** with examples at `/docs` and `/redoc`

> **Status.** This is an MVP. The default model backend is a deterministic mock so the whole
> stack runs anywhere in seconds; switch to a real model with one setting (§12, §13).
> Async jobs run inside the API process, the schema is managed by `create_all` (no
> migrations yet), and `response_format` (structured output) works only with chat models
> served through llama.cpp (§13); other backends reject it with `400`.

---

## 2. Architecture Diagram

The original specification described the project layout (see §11) but did not include an
architecture drawing, so the diagram below documents the request flow as implemented.

```
                ┌──────────────────────────────────────────────────────────────────────┐
  client ──────►│ FastAPI (Uvicorn workers)                                            │
  Bearer key    │  middleware: request ID · security headers · JSON logs (redacted)   │
                │                                                                      │
                │  POST /v1/vision                                                     │
                │   1. auth ─────────────► SHA-256(key) indexed lookup ──► PostgreSQL  │
                │   2. rate limit ───────► per-user INCR + EXPIRE ───────► Redis       │
                │   3. body read with hard byte cap → JSON | multipart                │
                │   4. images (thread pool):                                          │
                │        uploads ─► magic bytes ─► pixel limit ─► decode ─► orient     │
                │                   ─► strip metadata ─► downscale ─► re-encode        │
                │        image_url ─► SSRF-safe fetch (DNS pinned, public IPs only) ─┘ │
                │   5. VisionModel.analyze()  ── mock | transformers (CPU/GPU)         │
                │   6. usage row ─────────────────────────────────────────► PostgreSQL │
                │                                                                      │
                │  mode=async: job row ─► 202 + job_id; steps 4–6 run in background    │
                │  GET /v1/jobs/{id}: owner-scoped lookup ───────────────► PostgreSQL  │
                └──────────────────────────────────────────────────────────────────────┘
```

The web console reaches the same API through a reverse proxy (Docker Compose layout):

```
  browser ──► nginx :3000 ──► Next.js dashboard ──/api/*──► FastAPI :8000
              sets X-Forwarded-For   (fixed IP)            trusts XFF only from the dashboard IP
              = real peer address                          → sign-in throttling per real client IP
```

---

## 3. Tech Stack

| Area | Technology |
|------|------------|
| Language | Python 3.11+ (Docker image: 3.11) |
| Web framework | FastAPI, Starlette, Uvicorn (`uvloop`, `httptools`) |
| Validation & settings | Pydantic V2, pydantic-settings |
| Database | PostgreSQL 16, SQLAlchemy 2.0 (async ORM), asyncpg |
| Cache / rate limiting | Redis 7, redis-py (asyncio) |
| Dashboard auth | argon2-cffi (Argon2id passwords), cookie sessions with CSRF tokens |
| HTTP client | httpx (custom SSRF-safe fetcher) |
| Image processing | Pillow |
| Multipart parsing | python-multipart |
| Inference (optional) | PyTorch, Hugging Face Transformers, Accelerate |
| Testing | pytest, anyio, aiosqlite (in-memory SQLite), fakeredis |
| Packaging | Docker (multi-stage, non-root), Docker Compose |

---

## 4. Prerequisites

- **Docker** with **Docker Compose v2.24 or newer** (`docker compose version`). Docker Desktop
  includes both; the `docker-compose` commands below work with its v2 compatibility alias.
- **Python 3.11+** for local development and running tests without Docker.
- *(GPU only)* An NVIDIA GPU, a recent driver and the NVIDIA Container Toolkit (§13).

---

## 5. Quick Start (Docker)

From the project root (the directory containing this README):

```bash
cp .env.example .env
docker-compose up -d
docker-compose exec api python scripts/init_db.py
```

**API is now running at http://localhost:8000**, and the **web console at http://localhost:3000**
(create its first administrator as shown in §7, "Dashboard accounts").

Notes:

- The first `up` builds the image (≈1–2 minutes). The stack already runs a one-shot
  `init-db` service before the API starts, so the `exec … init_db.py` step is a safe,
  idempotent confirmation that prints `Tables are up to date.`
- Check health with `docker-compose ps` (the `api` service shows `(healthy)`) or
  `curl http://localhost:8000/health`.
- Inside Compose, the API reaches PostgreSQL and Redis by service name; those addresses
  override the `localhost` URLs in `.env` automatically.
- **Port already in use** (e.g. a local PostgreSQL on 5432)? Add to `.env`:
  `POSTGRES_HOST_PORT=5433`, `REDIS_HOST_PORT=6380`, `API_HOST_PORT=8080` or
  `DASHBOARD_HOST_PORT=3001`, then `docker-compose up -d` again.
- Stop with `docker-compose down`; `docker-compose down -v` also deletes the database volume.

Next: create an API key (§7) and send a request (§8).

---

## 6. Local Development Setup

```bash
# 1. Virtual environment and dependencies (dev file includes the runtime requirements)
python -m venv .venv
source .venv/bin/activate                # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt

# 2. Configuration
cp .env.example .env                     # Windows: copy .env.example .env

# 3. PostgreSQL and Redis via Docker Compose (bound to 127.0.0.1)
docker-compose up -d db redis

# 4. Create the tables
python scripts/init_db.py

# 5. Run the API with auto-reload
uvicorn app.main:create_app --factory --reload --reload-dir app
```

The defaults in `.env.example` match the Compose services
(`postgres:postgres@localhost:5432/vision_api`, `redis://localhost:6379/0`). If you use
your own PostgreSQL, set `VISION_DATABASE_URL` accordingly.

Without Redis the API still works: rate limiting fails open (and logs an error), but each
request waits up to `VISION_REDIS_SOCKET_TIMEOUT` (1 s) for the connection attempt.

---

## 7. Creating an API Key

Keys are issued by `scripts/create_api_key.py`. It creates the user (a default user unless
you pass `--email`; existing users are reused), generates the key, stores only its SHA-256
hash and prints the raw key:

```bash
# Docker
docker-compose exec api python scripts/create_api_key.py

# Local development
python scripts/create_api_key.py

# A key for a specific user
python scripts/create_api_key.py --email you@example.com
```

```
Created user default@vision-api.local
Key id:  6c2f0b8e-4a1d-4f5e-9c3b-2a7d8e6f1b0c
Expires: never
API key (shown only once; store it securely):
sk_live_Xy3...
```

Options: `--name laptop` (a label to tell keys apart) and `--expires-days 90`.

The key is printed **once**. Only its SHA-256 hash is stored, so a lost key cannot be
recovered. Revoke it with the printed key id and issue a new one:

```bash
python scripts/init_db.py revoke-key --key-id 6c2f0b8e-4a1d-4f5e-9c3b-2a7d8e6f1b0c
```

### Dashboard accounts

The web console (`../dashboard`, see its README) uses email + password accounts, separate
from API keys: a session cookie never authenticates `/v1/vision`, and an API key never signs
in to the dashboard. Customers see Overview, API keys, Usage, Billing and Settings;
administrators additionally get **Users** (disable accounts, change roles and plans, revoke
any key), **Plans** (prices and monthly quotas) and **Platform usage**.

```bash
# With Docker Compose, prefix these with: docker-compose run --rm api
# Create the first administrator. Prompts for the password; for unattended setups set
# VISION_ADMIN_PASSWORD instead (passwords are never accepted as command-line arguments).
python scripts/create_admin.py --email admin@example.com

# Promote an existing account without changing its password
python scripts/create_admin.py --email ada@example.com --keep-password
```

Regular users can sign up at `POST /v1/auth/signup` outside production; in production
signup is off unless `VISION_ALLOW_SIGNUP=true`. See the **auth** section of `/docs`.

---

## 8. Sending Your First Request

`POST /v1/vision` accepts **either** `multipart/form-data` (image files in the `images`
field, repeatable) **or** `application/json` (an `image_url`). Replace `sk_live_...` with your key.

**cURL (file upload):**

```bash
curl http://localhost:8000/v1/vision \
  -H "Authorization: Bearer sk_live_..." \
  -F prompt="Describe this image" \
  -F images=@photo.jpg
```

**cURL (image URL):**

```bash
curl http://localhost:8000/v1/vision \
  -H "Authorization: Bearer sk_live_..." \
  -H "Content-Type: application/json" \
  -d '{"prompt": "What is in this picture?", "image_url": "https://httpbin.org/image/png"}'
```

On Windows PowerShell, call `curl.exe` (not the `curl` alias).

**Python (httpx):**

```python
import httpx

with open("photo.jpg", "rb") as f:
    response = httpx.post(
        "http://localhost:8000/v1/vision",
        headers={"Authorization": "Bearer sk_live_..."},
        data={"prompt": "Describe this image", "max_tokens": "300"},
        files=[("images", ("photo.jpg", f, "image/jpeg"))],
        timeout=120,
    )
response.raise_for_status()
print(response.json()["result"]["text"])
```

**JavaScript (Node.js 18+, built-in `fetch`)**. Save as `first-request.mjs` and run
`VISION_API_KEY=sk_live_... node first-request.mjs`:

```javascript
import { readFile } from "node:fs/promises";

const form = new FormData();
form.append("prompt", "Describe this image");
form.append("images", new Blob([await readFile("photo.jpg")], { type: "image/jpeg" }), "photo.jpg");

const response = await fetch("http://localhost:8000/v1/vision", {
  method: "POST",
  headers: { Authorization: `Bearer ${process.env.VISION_API_KEY}` },
  body: form,
});
const body = await response.json();
if (!response.ok) throw new Error(`${response.status}: ${body.error.message}`);
console.log(body.result.text);
```

Call the API from a server, not from browser code: an API key embedded in a web page is public.

**Response** (default mock backend):

```json
{
  "id": "vis_3f2a9c1e7b4d4e0f9a6b8c2d1e0f3a4b",
  "object": "vision.response",
  "model": "mock-vision-1",
  "created": 1767225600,
  "result": {"text": "Mock analysis of 1 image(s) [4be1f02c9a31] for prompt: Describe this image"},
  "usage": {"input_tokens": 259, "output_tokens": 11}
}
```

### Request fields

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `prompt` | string, 1–8000 chars | required | Instruction or question; for Florence-2, a task such as `<OCR>` (see below). |
| `images` | file(s), multipart only | – | JPEG, PNG, WebP, GIF (first frame); ≤ 10 MB and ≤ 40 MP each. |
| `image_url` | string, ≤ 2048 chars | – | Public `http(s)` URL on port 80/443, fetched server-side. |
| `model` | string | `vision-default` | `vision-default` or the backend's model name. |
| `mode` | `sync` \| `async` | `sync` | `async` returns `202` + `job_id`. |
| `temperature` | number, 0–2 | `0.2` | `0` = greedy decoding. |
| `max_tokens` | integer, 1–4096 | `1000` | Maximum generated tokens. |
| `response_format` | JSON schema object | – | Output must be valid against this schema (`result.text` is then JSON). Chat models via llama.cpp only; others return `400`. |

### Florence-2 (OCR and captions)

With `VISION_MODEL_NAME=florence-community/Florence-2-large` (or `-base`), `prompt` must be one of
the model's tasks instead of a question; anything else returns `400` listing them:
`<OCR>` (all text), `<OCR_WITH_REGION>` and `<OD>` (JSON with pixel boxes), `<CAPTION>`,
`<DETAILED_CAPTION>`, `<MORE_DETAILED_CAPTION>`. Florence-2 sees images at 768×768, so `<OCR>`
reads tall pages in overlapping strips (`max_tokens` applies per strip; a line on a strip
boundary can appear twice). The dashboard lists the tasks via `GET /v1/dashboard/model`.

With curl, send the task with `--form-string` (`-F "prompt=<OCR>"` makes curl read a file named `OCR>`):

```bash
curl http://localhost:8000/v1/vision -H "Authorization: Bearer sk_live_..." \
  --form-string "prompt=<OCR>" -F max_tokens=1024 -F images=@invoice.jpg
```

At least one image is required; uploads plus URL may not exceed 4 (configurable).

### Invoice extraction

`POST /v1/invoices/extract` (API key, `multipart/form-data` field `file`) turns an invoice image
or PDF into structured JSON with **PaddleOCR-VL** (served locally by llama.cpp, see §13):
invoice number and date, seller and buyer (names, addresses, GSTINs), place of supply, line
items (description, HSN/SAC, quantity, unit, rate, discount, amount) and totals (subtotal,
CGST/SGST/IGST, round-off, total). Rate limits, quota and billing apply as for `/v1/vision`
(tokens of both models). The dashboard's **Invoices** page does the same in the browser.

```bash
curl http://localhost:8000/v1/invoices/extract -H "Authorization: Bearer sk_live_..." -F file=@invoice.pdf
python scripts/extract_invoice.py invoice.jpg --out invoice.json      # same, without the API
```

Every field carries `value` (`null` when not found, never guessed), `status`
(`ok` / `check` / `missing`), the `source_text` it was read from and, from Phase 4 on, a
`confidence`. GSTINs are checked with their checksum (OCR look-alikes such as `S`/`5` are
corrected only when the checksum confirms it). Per page the pipeline runs PaddleOCR-VL's
`Table Recognition:` on the whole page (header boxes, items, totals, tax summary), plus
`OCR:` on the top of the first page (seller and buyer blocks) and the bottom of the last
page (the "for <seller>" signature line). PDFs are rendered at 200 DPI (at most 10 pages,
20 MB); pages keep up to 2560 px because shrinking them first hurt table accuracy.

**Band reading** (with the reasoning model configured). Photos are first straightened (small
rotations). Qwen3-VL locates the item table's column-caption row; the rows below are cut into
overlapping bands of about 8-12 rows (cuts fall in the white gaps between text lines), the
caption row is pasted on top of every band, and each band is read at full resolution. Rows
read twice (whole page and band) are merged by line number, keeping the reading whose
quantity x rate matches its amount. The header OCR reaches down to the located caption row,
so tall seller/buyer blocks are not cut off. Rules cover Tally and common non-Tally layouts:
items by column caption, "Billing Address" / "To," / side-by-side "Bill to | Ship to" blocks,
captioned totals ("Grand Total", "IGST 5%", "Taxable Amount", stacked "Discount / IGST"
cells), round-off signs, mixed GST rates, GSTIN look-alikes confirmed by the checksum.

**Accuracy** is measured with `scripts/benchmark_invoices.py` against hand-checked answers
in `benchmarks/invoices/` (see its README; real invoices are kept out of version control;
`benchmarks/invoices/make_answers.py` writes the answer files). `--cached` re-applies the
current mapping rules to saved OCR output in seconds. Benchmark of 14 real bills from 12
suppliers (phone photos; Tally and 5 other layouts; 2 two-page invoices photographed page by
page; a freight bill), 1,455 printed fields:

| | exact | incl. near misses |
|---|---|---|
| Invoice details (number, date, place of supply, ...) | 89.3 % | 89.3 % |
| Parties (names, addresses, GSTINs) | 61.9 % | 81.0 % |
| Totals and taxes | 69.6 % | 69.6 % |
| Line items (HSN, quantity, unit, rate, discount, amount) | 90-99 % | 90-99 % |
| **Overall** | **90.6 %** | **92.1 %** |

Tally-style GST invoices score 93-99 %; the weak spots are a size-matrix layout (items
printed per size, 42 %) and the freight bill (27 %: its rows are consignments, not goods).
232 of 239 fields absent on the bills were correctly left empty. 5-15 min per page on a
4-core laptop CPU (8-15 model calls); well under a minute on a GPU.

**Reasoning model (optional).** With `VISION_EXTRACTION_LLM_URL` pointing to a second
llama-server running a small instruction model (default: Qwen3-VL-2B-Instruct), the model
also reads the OCR text and returns the header and party fields as schema-constrained JSON.
It never overrides the rules silently: agreement is noted as "confirmed by …"; any
disagreement, or a value only the model found, is marked `check`; model values that are not
in the OCR text, repeat another field or are printed boilerplate ("E & OE", amounts in words)
are dropped with a warning. Line items and tax amounts stay rule-based (exact and
self-checking). If the model is unreachable, extraction continues with the rules and says so.
On the benchmark invoice it confirmed 9 fields, flagged the 3 doubtful ones and changed no
correct value; it adds ~40 s per invoice on a laptop CPU.

**Zoomed crops (with the reasoning model).** PaddleOCR-VL shrinks every image to about one
megapixel, so small print on a whole page (or the wide header strip) gets few pixels per
character. The reasoning model boxes the seller, buyer, invoice-details and totals blocks;
each is cut out with a margin, enlarged to fill that megapixel (at most
`VISION_INVOICE_REREAD_SCALE`, default 2×) and read on its own. Then up to
`VISION_INVOICE_REREAD_MAX_FIELDS` (4) fields that are still `check`, fail a validation or are
required but missing are boxed one at a time and read again the same way. A zoomed reading
is a second opinion, never an automatic winner:

| Zoomed reading vs. the first reading | Result |
|---|---|
| agrees | note "matches the zoomed … crop" (no confidence bonus) |
| field was empty | zoomed value, `check` |
| differs; the first value fails its check (GSTIN checksum, date range, grand total) and the zoomed one passes | replaced, `check` |
| differs; the first value passes its check | kept; the other reading in the note |
| invoice number differs | `check`; replaced only when the block crop and the re-read agree with each other |
| name differs | note only (a clipped block and the signature line legitimately differ) |

A field flagged only because the block crop disagreed is cleared again when the re-read agrees
with the first value. The readings are saved in `raw_text` (`--- region OCR (…) ---`,
`--- re-read (…) ---`), so the benchmark can re-score them with `--cached` (and compare with
`--cached --no-zoom`). Cost: one locator call plus 3-4 OCR calls for the blocks, and a locator
plus an OCR call per re-read field, several minutes per page on a laptop CPU. Zoomed readings
are skipped once most of `VISION_INVOICE_TIMEOUT_SECONDS` is used, so they never cause a
timeout. **Item rows** that fail their arithmetic are read again in shorter bands (saved as
`--- items band (page N, re-read band K) ---`; `--no-zoom` leaves them out too).
`VISION_INVOICE_REGION_CROPS=false` and `VISION_INVOICE_REREAD_MAX_FIELDS=0` switch
them off.

Values are normalised: dates to `YYYY-MM-DD`, states to "Tamil Nadu (33)" (GST state codes),
units to `pcs`/`nos`/`kg`/…, and a financial-year slip such as `CR/134/26/27` to
`CR/134/26-27` when the invoice prints "26-27" elsewhere.

**Validation.** Every result carries a `validation` report (`passed` / `failed`, and each
check as `pass`, `fail` or `skip` with its message). A failed check marks the fields involved
as `check` with the reason; a check that cannot run (missing values, or a document that holds
only some pages of the invoice) is `skip`, never `pass`. Money comparisons allow 1 paisa.

| Check | Rule |
|-------|------|
| Required fields | invoice number, date, seller, seller GSTIN, buyer and total are present |
| Invoice date | between 1 Jul 2017 (GST start) and today |
| Seller / buyer GSTIN | format and checksum |
| Line numbers | consecutive (a gap or repeat means a row was dropped or read twice) |
| Line amounts | quantity × rate − discount % = amount, on every line |
| Items / quantities add up | lines sum to the taxable value and total quantity (only when line 1 is present) |
| Tax type | same state (seller GSTIN vs place of supply, else buyer GSTIN): CGST + SGST; different states: IGST |
| CGST = SGST | equal |
| Tax rate | total tax ÷ taxable value is a GST rate (0.1 … 40 %) |
| Round off / grand total | round-off under 1.00; taxable + taxes − discount ± round-off = total |
| Amounts in words | "Amount Chargeable (in words)" = total; "Tax Amount (in words)" = total tax (Indian numbering: lakh, crore, paise) |

On the benchmark invoice all 13 applicable checks pass (the two sums are skipped: it is page 2).
The benchmark script prints each invoice's validation next to its accuracy.

**Confidence and review.** Every field gets a heuristic `confidence` (not a calibrated
probability; tune the thresholds on the benchmark):

| Signal | Score |
|--------|-------|
| read by the rules (status `ok`) | 0.80 |
| + confirmed by the reasoning model | +0.10 |
| + vouched for by passed checks (e.g. GSTIN checksum, grand total, amount in words) | +0.05 each, at most +0.10 |
| marked `check` (disagreement, derived value) | 0.75 |
| involved in a failed check | 0.50 |
| corrected by a reviewer | 1.00 |

The invoice's confidence is the lowest among its key fields (number, date, seller, seller GSTIN,
buyer, buyer GSTIN, taxable value, total). Routing: at or above
`VISION_INVOICE_AUTO_APPROVE_CONFIDENCE` (0.90) with every check passed and no flagged line ->
**approved** automatically; at or above `VISION_INVOICE_REVIEW_CONFIDENCE` (0.70) -> **needs
review**; below -> **reprocess** (e.g. totals that do not add up). Only a second reader lifts a
field above 0.90, so without a reasoning model every invoice goes to review.

Extracted invoices are stored: the database keeps the result, and the upload plus its page
images go to `VISION_DOCUMENT_STORAGE_DIR` (default `./data/invoices`, a private Docker volume in
Compose; never serve it directly). The dashboard's **Invoices** page lists them by status and opens
a review screen: the page image beside editable fields and line items, confidence and notes per
field, the validation checks, and **Save corrections / Approve / Reprocess / Delete**. Every
correction is logged (field, old and new value, reviewer) in `invoice_corrections` for accuracy
analysis, and the invoice is validated and scored again from the original reading plus all
corrections, so a fix clears its flag and a wrong edit is flagged at once. Session endpoints:
`GET/PATCH/DELETE /v1/dashboard/invoices[/{id}]`, `GET .../{id}/pages/{n}`, `POST .../{id}/approve`,
`POST .../{id}/reprocess` (background; billed like an extraction).

**Background mode.** `POST /v1/invoices/extract?mode=async` stores the invoice and answers at
once with `202`, `{"id", "review_status": "processing", "status_url"}` and a `Location` header;
the reading continues in the background. Poll `GET /v1/invoices/{id}` (the dashboard's upload
page does this and opens the review page). Invoices interrupted by a server restart are marked
`failed` ("reprocess it") at the next start.

**Integration (ERP / accounting).** With an API key:

* `GET /v1/invoices?status=approved&updated_since=2026-09-01T00:00:00Z` lists the account's
  invoices, newest change first (poll with the time of your last sync);
* `GET /v1/invoices/{id}` returns one.

Both give plain values ready to import (`data`: amounts as decimal strings, ISO dates, `null`
when absent, `items` as a list), the review `status`, `confidence`, `validation` and `flags`
(paths of values still marked `check`). The dashboard exports the same records for the selected
tab as **Invoices CSV** (one row per invoice), **Line items CSV** (one row per item) or **JSON**
(`GET /v1/dashboard/invoices/export?format=csv|items_csv|json`); CSV cells that a spreadsheet
would run as formulas are prefixed with `'`.

**Approval webhook.** Set `VISION_INVOICE_WEBHOOK_URL` (and `VISION_INVOICE_WEBHOOK_SECRET`) to
have every approved invoice (automatic or by a reviewer) POSTed as
`{"event": "invoice.approved", "sent_at": ..., "invoice": <same shape as GET /v1/invoices/{id}>}`,
with headers `X-Vision-Event`, `X-Vision-Delivery` (the invoice id: ignore repeats) and
`X-Vision-Signature: sha256=<hex HMAC-SHA256 of the raw body>`. Three attempts (at once, after 2 s
and 10 s); non-2xx answers count as failures. Verify the signature before trusting the body:

```python
import hashlib, hmac

def valid(body: bytes, header: str, secret: str) -> bool:
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)
```

The URL is a server setting (an administrator's choice), never taken from API requests.

**Retention.** `VISION_INVOICE_FILE_RETENTION_DAYS=90` deletes the uploaded file and page images
of invoices older than 90 days (the extracted data and corrections stay; the review page says the
document was removed, and reprocessing is no longer possible). The API applies it at start-up and
every 6 hours; `python scripts/purge_invoice_files.py --days 90 [--dry-run]` does it on demand or
from cron / Task Scheduler.

**Thresholds (phase 6 finding).** On the 14-bill benchmark no invoice with a wrong key field
reaches auto-approval with the current rules; one did before a fix (an e-way bill number
taken as the invoice number, "confirmed" by the reasoning model because it misread the same
box). Until more bills have been measured, consider `VISION_INVOICE_AUTO_APPROVE_CONFIDENCE=0.95`
(or `1.01` to send everything to review).

**Purchase-entry details.** Besides the core `invoice`, each result carries `details`
(`InvoiceDetails`, `app/services/invoice_details.py`): IRN, ACK no/date, e-way bill, due date,
order/reference/challan numbers, agent, broker, LR no/date, transporter, destination, the
seller's PAN (from the GSTIN), phone, e-mail and bank (name, account, IFSC checked by format,
branch), seller/buyer state and code (from the GSTINs), CGST/SGST/IGST rates, tax total, gross
amount and the amount in words (checked against the total). They are read from the same OCR text
(no extra model calls), carry the same `value`/`status`/`note`, and take no part in validation
or routing. Benchmark: 83 % of 130 printed detail values exact.

**Invoice Tester.** `invoice-tester/` (next to `vision-api/`) is a stand-alone page for trying
the pipeline: API key field, upload, then a **Purchase form** (supplier & buyer, supplier bank,
invoice & transport, e-invoice, items grid, taxes & totals, GRN & notes) filled from the result,
editable, with values to check highlighted and ƒ columns calculated (taxable, amount, sell
price), exported as CSV or JSON; and an **Extraction details** tab (every field's status, the
checks, the OCR text). The form's layout is `invoice-tester/form_fields.json`: labels, order,
export keys and which extracted value fills each field. `vision-api\.venv\Scripts\python.exe
invoice-tester\server.py`, then open http://localhost:3002.

### Async mode

```bash
curl http://localhost:8000/v1/vision -H "Authorization: Bearer sk_live_..." \
  -F prompt="Describe this image" -F mode=async -F images=@photo.jpg
# 202 Accepted  (Location: /v1/jobs/<job_id>)
# {"job_id": "8d0c6f4e-…", "status": "pending", "result": null, "error": null}

curl http://localhost:8000/v1/jobs/<job_id> -H "Authorization: Bearer sk_live_..."
# {"job_id": "8d0c6f4e-…", "status": "completed", "result": { …same shape as a sync response… }, "error": null}
```

Jobs move `pending → processing → completed | failed`; a failed job's `error` holds a
client-safe reason. Only the job's owner can read it (anyone else gets `404`). Requests that
are invalid up front (no image, unknown model, bad parameters) are rejected immediately.

### Plans, charges and quotas

Every user is on a plan: the **default** plan unless an administrator assigns another. A plan
has prices (per 1K input tokens, per 1K output tokens, per image) and optional monthly limits
(requests and spend, per UTC calendar month). A "Default" plan with example prices ($0.0005 /
$0.0015 per 1K tokens, $0.002 per image, 5,000 requests and $25 per month) is created
automatically; change it with `PATCH /v1/admin/plans/{id}`.

- Each successful request is **priced when it runs** and the charge is stored with its usage
  record, so later price changes never rewrite past charges. Failed requests are free.
- When a limit is reached, `/v1/vision` answers `402 quota_exceeded_error` until the month
  resets. Limits are soft: requests already in progress may take usage slightly past them.
- Clients can check their standing with `GET /v1/usage` (API key); the dashboard uses
  `GET /v1/dashboard/billing`. Amounts are exact USD decimal strings, e.g. `"0.002000"`.

### Errors and rate limits

Every error on every endpoint has one shape:

```json
{"error": {"type": "rate_limit_error", "message": "Rate limit exceeded", "param": null},
 "request_id": "0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e"}
```

| Status | `type` | Typical cause |
|--------|--------|---------------|
| 400 | `invalid_request_error` | Invalid image, blocked URL, no image, unknown model, malformed JSON |
| 401 | `authentication_error` | Missing, malformed, unknown or revoked key |
| 402 | `quota_exceeded_error` | Monthly request quota or spend limit of your plan used up; resets on the 1st (UTC) |
| 403 | `permission_error` | Expired key or inactive user |
| 404 | `not_found_error` | Unknown route or job |
| 413 | `request_too_large` | Image or request body over the size limit |
| 415 | `invalid_request_error` | Content-Type is neither JSON nor multipart |
| 422 | `invalid_request_error` | Schema validation failed (`param` names the field) |
| 429 | `rate_limit_error` | Rate limit exceeded; wait `Retry-After` seconds |
| 502 | `model_error` | Model backend failed |
| 503 | `service_unavailable` | Model not loaded, or rate limiter down in fail-closed mode |
| 504 | `timeout_error` | Inference exceeded the timeout |

Successful `/v1/vision` responses carry `X-RateLimit-Limit`, `X-RateLimit-Remaining` and
`X-RateLimit-Reset` (seconds until the window resets). Every response has an `X-Request-ID`
header; quote it when reporting a problem.

---

## 9. API Documentation

With the API running (and `VISION_ENVIRONMENT` not `production`):

- **Swagger UI**: http://localhost:8000/docs. Click **Authorize**, paste your key, then use **Try it out** on any endpoint.
- **ReDoc**: http://localhost:8000/redoc
- **OpenAPI schema**: http://localhost:8000/openapi.json

Endpoint groups:

| Group | Auth | Endpoints |
|-------|------|-----------|
| **vision**, **jobs**, **usage** | API key (`Authorization: Bearer sk_live_...`) | `POST /v1/vision`, `GET /v1/jobs/{job_id}`, `GET /v1/usage` (plan, charges, remaining quota) |
| **auth** | none / session cookie | `/v1/auth/signup`, `login`, `logout`, `me`, `password` |
| **dashboard** | session cookie (+ `X-CSRF-Token` on writes) | `/v1/dashboard/keys` (list, create, rename, revoke), `/v1/dashboard/usage`, `/v1/dashboard/billing`, `/v1/dashboard/jobs` |
| **admin** | session cookie, `role=admin` | `/v1/admin/users` (list/search, activate, promote), `/v1/admin/users/{id}/keys`, `/v1/admin/keys/{id}`, `/v1/admin/usage`, `/v1/admin/plans` (list, create, update; assign with `PATCH /v1/admin/users/{id}`) |
| **health** | none | `GET /health` |

All three are disabled when `VISION_ENVIRONMENT=production`.

---

## 10. Running Tests

```bash
pip install -r requirements-dev.txt
pytest -v
```

The suite (300+ tests, ~10 s) needs **no** PostgreSQL, Redis, network or model weights. It
uses in-memory SQLite, an in-memory Redis injected into every test (a real Redis is never
touched), mocked HTTP transports and the mock model. It covers authentication, SSRF
(including DNS rebinding and redirect tricks), malicious and malformed images, the API
contract for both content types, rate limiting (including Redis outages), usage logging,
async job lifecycles and ownership, the CLI scripts, and the OpenAPI docs.

Run one area with, e.g., `pytest tests/test_security.py -v`.

---

## 11. Project Structure

```text
vision-api/
├── app/
│   ├── main.py                    # App factory, lifespan (model + Redis), middleware, error handlers
│   ├── api/
│   │   ├── dependencies.py        # API-key auth, session auth + CSRF, admin guard
│   │   └── routes/
│   │       ├── admin.py           # /v1/admin: users, any key, platform usage
│   │       ├── auth.py            # /v1/auth: signup, login, logout, me, password
│   │       ├── dashboard.py       # /v1/dashboard: own keys, usage, billing, jobs
│   │       ├── dashboard_invoices.py  # /v1/dashboard/invoices: review, correct, approve
│   │       ├── health.py          # GET /health
│   │       ├── vision.py          # POST /v1/vision (sync + async, JSON + multipart)
│   │       ├── invoices.py        # POST /v1/invoices/extract
│   │       ├── jobs.py            # GET /v1/jobs/{job_id}
│   │       └── usage.py           # GET /v1/usage (billing overview for API clients)
│   ├── core/
│   │   ├── config.py              # Settings (VISION_* environment variables)
│   │   ├── logging.py             # JSON logging with secret redaction
│   │   ├── security.py            # API keys, passwords, tokens, SSRF-safe fetcher
│   │   └── rate_limit.py          # Redis fixed-window limiters (API + sign-in)
│   ├── models/
│   │   ├── base.py                # VisionModel interface + model errors
│   │   ├── llama_cpp.py           # GGUF models via a local llama-server (PaddleOCR-VL)
│   │   └── local_transformers.py  # Transformers backends (chat, Florence-2) + MockVisionModel
│   ├── services/
│   │   ├── auth_service.py        # Session lifecycle and cookies
│   │   ├── billing_service.py     # Plans, pricing, quotas, billing overview
│   │   ├── document_service.py    # Image/PDF -> sanitised page images
│   │   ├── image_service.py       # Validation, metadata stripping, resizing
│   │   ├── invoice_extraction.py  # PaddleOCR-VL pipeline + rule-based field mapping
│   │   ├── invoice_llm.py         # Reasoning-model cross-check of the fields
│   │   ├── invoice_normalize.py   # GST state codes, units, invoice-number fixes
│   │   ├── invoice_validation.py  # Arithmetic, GST and amount-in-words checks
│   │   ├── invoice_confidence.py  # Confidence per field, routing
│   │   ├── invoice_service.py     # Stored invoices: extract, store, correct, reprocess
│   │   ├── document_store.py      # Uploaded files and page images on disk
│   │   ├── invoice_export.py      # Plain values, CSV exports (integration format)
│   │   ├── invoice_integration.py # Approval webhook, retention, restart recovery
│   │   ├── vision_service.py      # Orchestration + async job runner
│   │   └── usage_service.py       # Usage logging + dashboard aggregations
│   ├── database/
│   │   ├── database.py            # Async engine, sessions
│   │   └── models.py              # User, UserSession, APIKey, Plan, UsageLog, Job, InvoiceRecord, InvoiceCorrection
│   └── schemas/
│       ├── auth.py                # Account/session schemas
│       ├── common.py              # UTC timestamps, UUID pattern
│       ├── dashboard.py           # Keys, usage reports, jobs, admin views
│       ├── invoice.py             # Structured invoice (value + status per field)
│       ├── usage.py               # Plans and billing overview
│       └── vision.py              # Request/response/error schemas
├── scripts/
│   ├── init_db.py                 # Create tables; revoke keys
│   ├── create_api_key.py          # Create a user + API key
│   ├── create_admin.py            # Create/promote a dashboard administrator
│   ├── extract_invoice.py         # Invoice image/PDF -> structured JSON
│   ├── benchmark_invoices.py      # Field-level accuracy against hand-checked answers
│   └── purge_invoice_files.py     # Retention: delete old invoice files
├── benchmarks/invoices/           # Benchmark invoices + answers (private, git-ignored)
├── deploy/nginx/dashboard.conf    # Reverse proxy in front of the web console (client IPs)
├── tests/                         # pytest suite (test_deploy.py guards the proxy wiring)
├── .env.example
├── requirements.txt               # Runtime dependencies
├── requirements-dev.txt           # + test dependencies
├── pyproject.toml                 # pytest configuration
├── Dockerfile                     # Multi-stage, non-root image
├── docker-compose.yml             # PostgreSQL, Redis, init-db, API, dashboard + nginx
├── docker-compose.gpu.yml         # Optional NVIDIA GPU override
└── README.md
```

---

## 12. Configuration

The API is configured through environment variables prefixed with `VISION_`, read from the
process environment and from `.env` in the working directory (template: `.env.example`).
List values use JSON syntax, e.g. `VISION_FETCH_ALLOWED_PORTS=[80,443]`.

**Application**

| Variable | Default | Description |
|----------|---------|-------------|
| `VISION_ENVIRONMENT` | `development` | `development`, `test` or `production`. Production disables `/docs`, `/redoc` and `/openapi.json` and refuses to start with the mock model. |
| `VISION_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`. |
| `VISION_APP_NAME` / `VISION_APP_VERSION` | `Vision AI API` / `0.1.0` | Shown in the docs and `/health`. |

**Database and Redis**

| Variable | Default | Description |
|----------|---------|-------------|
| `VISION_DATABASE_URL` | `postgresql+asyncpg://postgres:postgres@localhost:5432/vision_api` | Must use the `postgresql+asyncpg://` driver. URL-encode special characters in the password. |
| `VISION_DATABASE_POOL_SIZE` | `10` | Connections per worker process. |
| `VISION_DATABASE_MAX_OVERFLOW` | `20` | Extra connections under load. |
| `VISION_DATABASE_ECHO` | `false` | Log every SQL statement (debugging only). |
| `VISION_REDIS_URL` | `redis://localhost:6379/0` | Use `rediss://` with a password in production. |
| `VISION_REDIS_SOCKET_TIMEOUT` | `1.0` | Seconds; bounds added latency when Redis is slow or down. |

**Dashboard accounts and sessions**

| Variable | Default | Description |
|----------|---------|-------------|
| `VISION_ALLOW_SIGNUP` | *(empty)* | Empty: self-service signup outside production only. `true`/`false` forces it. |
| `VISION_SESSION_TTL_HOURS` | `168` | Absolute session lifetime (7 days). |
| `VISION_SESSION_IDLE_TIMEOUT_HOURS` | `24` | Sign out after this long without activity (must not exceed the TTL). |
| `VISION_SESSION_COOKIE_SECURE` | *(empty)* | Empty: `Secure` cookies in production only. Set `true` whenever the dashboard is served over HTTPS. |
| `VISION_SESSION_COOKIE_NAME` / `VISION_CSRF_COOKIE_NAME` | `vision_session` / `vision_csrf` | Cookie names. |
| `VISION_LOGIN_RATE_LIMIT_ATTEMPTS` | `10` | Sign-in/sign-up attempts per email per window (per IP: 3x). Always on. |
| `VISION_LOGIN_RATE_LIMIT_WINDOW_SECONDS` | `900` | Window for the sign-in limits. |
| `VISION_MAX_API_KEYS_PER_USER` | `25` | Active API keys a user may hold (revoked keys do not count). |

Empty values (`VISION_X=`) mean *use the default*. Do not put a trailing `# comment` after
an empty value in `.env`: the parser would read the comment as the value.

**Rate limiting**

| Variable | Default | Description |
|----------|---------|-------------|
| `VISION_RATE_LIMIT_ENABLED` | `true` | Turn rate limiting on or off. |
| `VISION_RATE_LIMIT_REQUESTS` | `10` | Requests allowed per user per window. |
| `VISION_RATE_LIMIT_WINDOW_SECONDS` | `60` | Window length. |
| `VISION_RATE_LIMIT_FAIL_OPEN` | `true` | If Redis is unreachable: `true` allows requests, `false` returns `503`. |

**Model**

| Variable | Default | Description |
|----------|---------|-------------|
| `VISION_USE_MOCK_MODEL` | `true` | `false` loads `VISION_MODEL_NAME` with transformers (needs `torch torchvision transformers accelerate`). |
| `VISION_MODEL_NAME` | `HuggingFaceTB/SmolVLM-256M-Instruct` | Hugging Face model id (image-text-to-text). Chat models (SmolVLM, Qwen-VL) answer questions; Florence-2 (`florence-community/Florence-2-large`) runs fixed tasks such as `<OCR>`. The backend is chosen from the model's configuration. |
| `VISION_MODEL_DEVICE` | `auto` | `auto`, `cpu` or `cuda`. |
| `VISION_MODEL_BACKEND` | `transformers` | `llama_cpp` serves a GGUF model through a local llama.cpp `llama-server` (§13); `VISION_MODEL_NAME` is then only a label, e.g. `PaddleOCR-VL-1.6-GGUF` (names containing "PaddleOCR-VL" get its tasks). |
| `VISION_LLAMA_CPP_URL` | `http://127.0.0.1:8080` | Where `llama-server` listens. The API checks it at startup. |
| `VISION_MODEL_TIMEOUT_SECONDS` | `120` | Per-request inference deadline (`504` when exceeded; the abandoned generation is stopped). Use ~`300` for CPU inference; the dashboard waits up to 330 s. |
| `VISION_INVOICE_TIMEOUT_SECONDS` | `1800` | Deadline for one whole invoice extraction (header, tables, bands, footer, cross-check, zoomed crops: 8-25 model calls). `504` when exceeded. |

**Images**

| Variable | Default | Description |
|----------|---------|-------------|
| `VISION_MAX_IMAGES_PER_REQUEST` | `4` | Uploads plus URL. |
| `VISION_MAX_IMAGE_BYTES` | `10485760` | Per image (10 MB). |
| `VISION_MAX_IMAGE_PIXELS` | `40000000` | Rejects decompression bombs before decoding. |
| `VISION_MAX_IMAGE_SIDE` | `1536` | Images are downscaled to fit this box. |
| `VISION_ALLOWED_IMAGE_FORMATS` | `["JPEG","PNG","WEBP","GIF"]` | Subset of JPEG, PNG, WEBP, GIF, BMP, TIFF. |
| `VISION_MAX_DOCUMENT_BYTES` / `VISION_MAX_DOCUMENT_PAGES` | `20971520` / `10` | Invoice extraction: PDF size and page limits. |
| `VISION_MAX_DOCUMENT_SIDE` | `2560` | Invoice pages keep up to this many pixels (lossless PNG); the OCR model resizes them itself. |
| `VISION_EXTRACTION_LLM_URL` | *(empty)* | Second llama-server with a reasoning model that cross-checks invoice fields; empty = rules only. |
| `VISION_EXTRACTION_LLM_NAME` / `VISION_EXTRACTION_LLM_TIMEOUT_SECONDS` | `Qwen3-VL-2B-Instruct` / `300` | Label shown in results and notes; per-call timeout. |
| `VISION_INVOICE_REGION_CROPS` | `true` | Read the seller, buyer, invoice-details and totals blocks as zoomed crops (needs the reasoning model; see "Zoomed crops"). |
| `VISION_INVOICE_REREAD_MAX_FIELDS` | `4` | Flagged fields re-read as zoomed crops per invoice; `0` = off. |
| `VISION_INVOICE_REREAD_SCALE` | `2.0` | Largest enlargement of a zoomed crop (1-4); crops are never enlarged past the OCR model's ~1 megapixel input. |
| `VISION_INVOICE_REREAD_ITEMS` | `true` | When line amounts, the items total or the line numbers fail their checks, read the item table again in shorter bands (about 8% of the page instead of 15%: cut elsewhere, read without shrinking). A re-read row replaces the first reading only if its quantity x rate matches its amount. |
| `VISION_INVOICE_AUTO_APPROVE_CONFIDENCE` / `VISION_INVOICE_REVIEW_CONFIDENCE` | `0.9` / `0.7` | Routing thresholds (see "Confidence and review"). |
| `VISION_INVOICE_FILE_RETENTION_DAYS` | *(empty)* | Delete uploaded invoice files and page images after this many days (data is kept); empty = keep. |
| `VISION_INVOICE_WEBHOOK_URL` / `VISION_INVOICE_WEBHOOK_SECRET` | *(empty)* | POST approved invoices to this URL, signed with HMAC-SHA256 using the secret (see "Approval webhook"). |
| `VISION_DOCUMENT_STORAGE_DIR` | `./data/invoices` | Uploaded invoices and page images (Compose: the `invoices` volume at `/data/invoices`). Private: served only through the owner-checked dashboard endpoint. |
| `VISION_OUTPUT_JPEG_QUALITY` | `90` | Quality when re-encoding (PNG inputs stay PNG). |

**Remote image fetching (`image_url`)**

| Variable | Default | Description |
|----------|---------|-------------|
| `VISION_FETCH_ALLOWED_SCHEMES` | `["http","https"]` | Allowed URL schemes. |
| `VISION_FETCH_ALLOWED_PORTS` | `[80,443]` | Allowed destination ports. |
| `VISION_FETCH_CONNECT_TIMEOUT` | `3.0` | Seconds. |
| `VISION_FETCH_READ_TIMEOUT` | `5.0` | Seconds. |
| `VISION_FETCH_TOTAL_TIMEOUT` | `5.0` | Seconds for the whole fetch, including redirects. |
| `VISION_FETCH_MAX_REDIRECTS` | `3` | Each redirect is re-validated. |
| `VISION_FETCH_MAX_URL_LENGTH` | `2048` | Characters. |
| `VISION_FETCH_USER_AGENT` | `VisionAPI-Fetcher/1.0` | Sent to image hosts. |

**Docker / Compose only** (not read by the app)

| Variable | Default | Description |
|----------|---------|-------------|
| `WEB_CONCURRENCY` | `2` | Uvicorn worker processes in the container (use `1` with a real model). |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | `postgres` / `postgres` / `vision_api` | Database created by Compose; change the password for anything shared. |
| `POSTGRES_HOST_PORT` / `REDIS_HOST_PORT` / `API_HOST_PORT` | `5432` / `6379` / `8000` | Host ports (PostgreSQL and Redis bind to 127.0.0.1 only). |
| `DASHBOARD_HOST_PORT` | `3000` | Host port of the web console (the nginx proxy; the Next.js container is never published). |
| `PUBLIC_API_URL` | `http://localhost:8000` | API address shown in the console's code samples (baked in at build time: rebuild after changing). |
| `COMPOSE_SUBNET` / `DASHBOARD_IP` | `172.28.0.0/24` / `172.28.0.10` | Compose network and the dashboard's fixed address, the only peer whose `X-Forwarded-For` the API trusts. Change together if the subnet clashes with a local network. |
| `TORCH_INDEX_URL` | CUDA 12.8 wheels (GPU override only) | PyTorch wheel index for the GPU build (§13). |

---

## 13. GPU Deployment

1. **Host setup**: install the NVIDIA driver and the
   [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html),
   then confirm containers can see the GPU:

   ```bash
   docker run --rm --gpus all nvidia/cuda:12.8.0-base-ubuntu24.04 nvidia-smi
   ```

2. **Choose a model**, in `.env`:

   ```bash
   VISION_MODEL_NAME=HuggingFaceTB/SmolVLM-256M-Instruct   # or any image-text-to-text model that fits your GPU
   ```

3. **Start with the GPU override**, which builds CUDA-enabled PyTorch into the image, sets
   `VISION_USE_MOCK_MODEL=false`, `VISION_MODEL_DEVICE=cuda` and `WEB_CONCURRENCY=1`, and
   reserves one GPU:

   ```bash
   docker-compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build
   ```

   The image uses CUDA 12.8 PyTorch wheels by default. If `nvidia-smi` reports an older
   CUDA version, set `TORCH_INDEX_URL` to a matching index (see https://pytorch.org/get-started/locally/).

4. The first start downloads the weights into the `models` volume (reused afterwards);
   follow progress with `docker-compose logs -f api`.

Without Docker: `pip install torch torchvision transformers accelerate` (CUDA build from pytorch.org),
then run with `VISION_USE_MOCK_MODEL=false` and `VISION_MODEL_DEVICE=cuda`.

### GGUF models with llama.cpp (PaddleOCR-VL)

Quantised GGUF models run in llama.cpp's `llama-server`, a separate local process that the
API calls over HTTP (images never leave the machine). On a CPU this is much faster than
PyTorch; on NVIDIA GPUs use llama.cpp's CUDA build. For invoices use the official
[PaddleOCR-VL-1.6-GGUF](https://huggingface.co/PaddlePaddle/PaddleOCR-VL-1.6-GGUF)
(Apache-2.0; `PaddleOCR-VL-1.6-GGUF.gguf` + `PaddleOCR-VL-1.6-GGUF-mmproj.gguf`, 1.8 GB):

```bash
# llama.cpp: https://github.com/ggml-org/llama.cpp/releases (win-cpu-x64, or win-cuda for NVIDIA)
llama-server -m PaddleOCR-VL-1.6-GGUF.gguf --mmproj PaddleOCR-VL-1.6-GGUF-mmproj.gguf \
  --temp 0 -c 16384 --host 127.0.0.1 --port 8090    # add -ngl 99 -fa on on a GPU build
```

On a GPU build keep the default `VISION_MODEL_TIMEOUT_SECONDS=120` (a whole-page `OCR:` call
takes ~12 s on an 8 GB RTX A1000); on a CPU raise it to ~600. The OCR server and the Qwen3-VL-2B
reasoning server below fit on one 8 GB card together (~6 GB).

```bash
# .env
VISION_USE_MOCK_MODEL=false
VISION_MODEL_BACKEND=llama_cpp
VISION_MODEL_NAME=PaddleOCR-VL-1.6-GGUF
VISION_LLAMA_CPP_URL=http://127.0.0.1:8090
```

`/v1/vision` then accepts PaddleOCR-VL's tasks as `prompt` (`OCR:`, `Table Recognition:`,
`Chart Recognition:`, `Formula Recognition:`, `Seal Recognition:`), and the dashboard
Playground offers them in its task list. Keep `llama-server` bound to 127.0.0.1: it has no
authentication.

For the invoice reasoning model, run a second server with
[Qwen3-VL-2B-Instruct-GGUF](https://huggingface.co/Qwen/Qwen3-VL-2B-Instruct-GGUF) (Apache-2.0;
`Qwen3VL-2B-Instruct-Q4_K_M.gguf` + `mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf`, 1.55 GB) and set
`VISION_EXTRACTION_LLM_URL=http://127.0.0.1:8091`:

```bash
llama-server -m Qwen3VL-2B-Instruct-Q4_K_M.gguf --mmproj mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf \
  --temp 0 -c 16384 --host 127.0.0.1 --port 8091
```

Served as the main model (`VISION_MODEL_NAME=Qwen3-VL-2B-Instruct`), it also answers
free-form questions on `/v1/vision` and supports `response_format`.

**Scaling note:** the in-process backend runs one generation at a time per process, and
every Uvicorn worker loads its own copy of the weights. For production throughput, serve
the model from a dedicated inference server (vLLM or TGI) and add a `VisionModel` client
for it (`app/models/base.py` defines the interface).

---

## 14. Security Notes

- **API key hashing**: keys (`sk_live_` + 192 random bits) are shown once and stored only as
  SHA-256 hashes; lookups use the hash. Revoked, expired and inactive-user keys are rejected.
- **SSRF protection**: `image_url` must be `http(s)` on port 80/443 with no embedded
  credentials. The host is resolved once, every address must be publicly routable
  (loopback, private, link-local and cloud-metadata ranges are blocked, including
  IPv4-mapped/NAT64/6to4/Teredo forms), and the connection is pinned to the vetted IP, which
  defeats DNS rebinding. Redirects are re-checked hop by hop; proxies from the environment
  are ignored; downloads are capped in size and time.
- **EXIF stripping and image safety**: files are identified by content (magic bytes), never
  by name. Pixel counts are checked before decoding (decompression bombs). Every image is
  fully decoded and re-encoded from raw pixels, which removes EXIF (including GPS), ICC
  profiles, text chunks and anything appended to the file; orientation is applied first.
- **Dashboard accounts**: Argon2id password hashing (hashes upgraded on login when parameters
  change), identical errors and timing for unknown emails and wrong passwords, sign-in
  throttling per IP and per email, server-side sessions (only token hashes stored) in httpOnly
  `SameSite=Lax` cookies with absolute and idle expiry, CSRF tokens bound to the session and
  required on every state-changing request, and other sessions revoked on password change.
- **Client IPs behind the console**: Next.js neither adds `X-Forwarded-For` nor removes one
  sent by the browser. Compose therefore puts nginx in front of it, which *replaces* the
  header with the real peer address, and the API (`uvicorn --proxy-headers`,
  `FORWARDED_ALLOW_IPS`) honours the header only from the dashboard's fixed IP. Direct API
  clients cannot spoof their address, and never expose the dashboard container's port
  directly. With another load balancer in front of nginx, enable nginx's `real_ip` module
  for it (see `deploy/nginx/dashboard.conf`).
- **Rate limiting**: per-user fixed window in Redis, checked after authentication and
  before any image processing; rate-limited requests are neither processed nor billed.
- **Other**: strict request schemas (unknown fields rejected), hard caps on body size
  (including chunked uploads), a single error format that never exposes internals, JSON
  logs that redact keys, tokens and credentials and never log query strings or image URLs,
  security headers, and a non-root container with read-only application code.

**For production**, also: set `VISION_ENVIRONMENT=production`, use strong database and
Redis credentials (`rediss://`), terminate TLS in front of the API, pin the base image by
digest, and back up the database (usage logs are your billing record). Serve the console
over HTTPS with `VISION_SESSION_COOKIE_SECURE=true` (the default in production).

---

## 15. License & Model Attribution

**Project license**: no license file is included yet. Add one (e.g. `LICENSE`) before
distributing this code.

**Model licenses are separate and are your responsibility.** Model weights are not part of
this repository; they are downloaded from Hugging Face when a real model is enabled, under
their own licenses. Before deploying any VLM, and especially before commercial use:

- read the **license on the model card** for the exact model id you configure (licenses
  differ between model families *and* between sizes of the same family),
- check the licenses of the model's **training data and any base models** it derives from,
  where the card lists them,
- check for **use restrictions** (acceptable-use policies, user-count thresholds, regional
  or field-of-use limits) and **attribution requirements**, and
- keep a record of the model id, revision and license you deployed.

The default `HuggingFaceTB/SmolVLM-256M-Instruct` is distributed under the Apache 2.0
license at the time of writing; verify this on its model card before relying on it. The
same applies to the models used for OCR and invoices: `florence-community/Florence-2-large`
(MIT), `PaddlePaddle/PaddleOCR-VL-1.6-GGUF` and `Qwen/Qwen3-VL-2B-Instruct-GGUF` (both
Apache 2.0), all at the time of writing.
llama.cpp itself is MIT-licensed.
