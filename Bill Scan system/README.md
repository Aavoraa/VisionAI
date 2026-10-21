# Daily Bills

Photograph a daily bill, have it read, check the arithmetic, fix what is
wrong, save it.

The point of the system is not the extraction — it is the **reconciliation
layer**. A vision model reading a crumpled receipt will occasionally turn 100
into 1000, and nothing about the returned JSON looks any different when it
does. But a bill carries its own proof: the lines multiply out, they sum to
the subtotal, and the subtotal plus tax plus rounding is the grand total. Every
one of those is checked against the model's own numbers, so a single misread
digit is caught without any ground truth to compare against.

Whatever fails the check is highlighted next to the original photo, and a human
decides.

```
┌───────────────────────────────────────────────────────────────┐
│  [← Back]   Scan Daily Bill                    [Save] [Train] │
├───────────────────────────────────────────────────────────────┤
│  ⚠  2 math issues found: grand_total doesn't match  [details]  │
├──────────────────────────┬────────────────────────────────────┤
│                          │  Vendor:  [ABC Store        ]      │
│                          │  Date:    [2026-08-24       ]      │
│                          │  Category:[groceries ▼      ]      │
│   ORIGINAL IMAGE         │  Payment: [cash ▼           ]      │
│   (zoom, pan, rotate)    │                                    │
│                          │  ── Line Items ──── [+ Add line]   │
│                          │  ┌────┬─────┬─────┬─────┬─────┐    │
│                          │  │Item│Qty  │Rate │Amt  │Tax  │    │
│                          │  ├────┼─────┼─────┼─────┼─────┤    │
│                          │  │Milk│ 2   │ 30  │ 60  │  —  │    │
│                          │  │Bred│ 1   │ 40  │ 40  │  —  │    │
│                          │  └────┴─────┴─────┴─────┴─────┘    │
│                          │                                    │
│                          │  Subtotal:    [ 100 ]              │
│                          │  Tax:         [   0 ]              │
│                          │  Grand Total: [1000 ]  ← red       │
└──────────────────────────┴────────────────────────────────────┘
```

## Layout

```
backend/
  app/
    main.py                 FastAPI app
    config.py               settings from the environment
    models.py               SQLAlchemy models + engine
    schemas.py              Pydantic request/response schemas
    extraction/
      prompt.py             the prompt and the enforced JSON schema
      claude_vision.py      the vision call
      validate.py           the reconciliation layer
    routers/
      bills.py              scan, review, confirm, train
  tools/
    validate_test.py        checks the reconciliation layer
    api_smoke_test.py       checks the HTTP surface, no network needed
  data/
    daily_bills.db          created on first run
    uploads/                the original photos
frontend/
  src/
    api.js                  one place that talks to the backend
    components/
      BillUploader.jsx      drop, pick, or paste a photo
      ImageViewer.jsx       zoom, pan, rotate the original
      BillReviewForm.jsx    the editable bill
      ValidationErrorBanner.jsx
    pages/
      BillListPage.jsx      everything scanned so far
      ScanBillPage.jsx      the side-by-side review screen
```

## Running it

**Backend** (Python 3.11+):

```bash
cd backend
python -m venv .venv && .venv\Scripts\activate      # PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env                              # then put your key in it
uvicorn app.main:app --reload
```

`ANTHROPIC_API_KEY` is read from the environment. An `ant auth login` profile
works too — the SDK finds it without any code change.

**Frontend** (Node 18+):

```bash
cd frontend
npm install
npm run dev          # http://localhost:5173, proxies /api to :8000
```

**Tests** — both run offline and cost nothing:

```bash
cd backend
python -m tools.validate_test     # 25 checks on the reconciliation layer
python -m tools.api_smoke_test    # 33 checks on the HTTP surface, vision stubbed
```

## How a bill moves through the system

1. **Upload.** The photo is saved to disk *before* the extraction runs, so a
   failed call never loses the bill.
2. **Extract.** One vision call. The response shape is enforced by the API via
   `output_config.format`, so there is no markdown-fence stripping or
   best-effort JSON repair anywhere in the codebase — the model cannot return
   prose or omit a key.
3. **Reconcile.** `validate.py` checks the model's numbers against each other
   and returns a list of flags, each naming a field path (`grand_total`,
   `line_items[1].amount`). Any hard failure caps the stored confidence at
   `low` regardless of how confident the model claimed to be.
4. **Review.** The photo and the form sit side by side; flagged fields are red.
   Editing re-runs the *same* backend validator against the edited bill, so the
   highlights track what is on screen and the two can never drift apart.
5. **Confirm.** The reviewed bill is stored. What the model originally returned
   is kept in `raw_extraction`, along with which fields the human changed, so
   extraction quality can be measured later rather than guessed at.

Saving is allowed with flags still showing. A printed bill really can be wrong,
and the person holding it outranks the arithmetic; the flags are stored either
way so the discrepancy stays visible.

## What the checks catch

| Check | Severity | Example |
|---|---|---|
| qty × rate ≠ amount | error | `2 × 30 = 60, but amount is 90` |
| line amounts ≠ subtotal | error | offers the line sum as a correction |
| subtotal + tax + round_off ≠ grand_total | error | the misread-digit case |
| grand_total ≤ 0 | error | |
| CGST + SGST + IGST ≠ tax_amount | warning | |
| no line items, or the image is not a bill | error | |
| date unparseable, or in the future | warning | a misread year |
| GSTIN not 15 characters | warning | |

Two properties are worth knowing about:

- **`validate_bill` never mutates the bill it is given.** Corrections the math
  suggests come back in `result.corrections`; only
  `apply_validation(autocorrect=True)` writes them.
- **A wrong subtotal does not cascade.** If the lines and the grand total agree
  with each other and only the printed subtotal is off, just the subtotal is
  flagged. Flagging the grand total too would bury the real problem in noise.

## Getting the data into Excel

Both exports carry whatever filters are set on the list page, so "confirmed
groceries in August" exports as exactly that.

**Download** — the *Download Excel* button gives a workbook with two sheets:
*Bills* (one row per bill, with a SUM row under each money column) and *Line
Items* (one row per item, carrying its bill's date and vendor so the sheet
stands alone). Headers are frozen and filtered.

**Live link** — *link to Excel* shows a URL to paste into Excel via
**Data → Get Data → From Other Sources → From Web**. After loading it once,
**Refresh All** re-pulls the current bills; nothing needs re-importing. Pick
one row per bill or one row per line item.

```
GET /api/bills.xlsx?status=&category=&date_from=&date_to=
GET /api/bills.csv?...&granularity=bills|lines
```

Two things worth knowing:

- The CSV is UTF-8 **with a BOM**. Without it Excel reads the file as the
  system codepage and mangles any non-ASCII vendor name.
- **Google Sheets cannot read the live link.** `IMPORTDATA` and friends run on
  Google's servers, which cannot reach a server on your machine. To get bills
  into Sheets, download the workbook and upload it, or host this app somewhere
  public.

## Save & Train Vendor

Confirming with **Train** writes a vendor profile: the GSTIN, the usual
category and payment method, and the item descriptions seen on that shop's
bills. Subsequent scans carry the roster of trained vendors into the prompt, so
a smudged shop name, an abbreviated item, or the right category can be resolved
from what a human already confirmed.

The roster goes in on the *first* pass. The vendor is not known until the bill
has been read, so matching it afterwards would mean paying for a second
extraction of every bill; instead the model is handed the list up front and
matches the name itself. Vendor names are keyed with punctuation and spacing
stripped, so `A.B.C. STORE` and `ABC Store` train the same profile.

## Configuration

| Variable | Default | |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | or an `ant auth login` profile |
| `BILLS_MODEL` | `claude-opus-5` | |
| `BILLS_EFFORT` | `medium` | raise to `high` for hard handwriting |
| `BILLS_MAX_UPLOAD` | 15 MB | |
| `BILLS_CORS_ORIGINS` | `http://localhost:5173` | comma-separated |

## Notes

- Images are rotated by their EXIF tag, downscaled to 1568 px, and re-encoded
  as JPEG before the call. Phone photos arrive sideways otherwise, and anything
  larger costs tokens without adding readable detail.
- The stable half of the system prompt is marked for caching; vendor hints go
  after the breakpoint so adding a vendor never invalidates the cached prefix.
- Line items are stored as JSON on the bill rather than in their own table.
  They are only ever read together with their bill, and the schema of an
  extracted line changes more easily than a migration allows.
