# Performance analysis — invoice extraction pipeline

Phase 1 of the performance work: how the pipeline actually runs today, read from the code
(`app/services/invoice_extraction.py`, `InvoiceExtractor.read`), before any behaviour change.
Timings are filled in from the baseline run (`benchmarks/performance/baseline.json`).

## 1. Current pipeline (in order, all sequential)

| # | Stage (profile key) | Work | Model |
|---|---|---|---|
| 0 | upload | multipart read, `load_pages`: PDF → PNG (pypdfium2), images re-encoded, long side ≤ 2,560 px | — (CPU) |
| 1 | `deskew` | per page, projection-profile angle search (NumPy), rotate | — (CPU, threads) |
| 2 | `table_detection` | `locate_table`: page 1..n shrunk to 1,280 px JPEG, JSON box of the item table | Qwen3-VL-2B, 1 vision call / page |
| 3 | `header_ocr` | crop top 35–60 % of page 1, `OCR:` with DRY anti-repetition; retried once if it loops | PaddleOCR-VL, 1–2 calls |
| 4 | `table_ocr` | every whole page, `Table Recognition:` (≤ 4,096 tokens) | PaddleOCR-VL, 1 / page |
| 5 | `item_band_ocr` | located table cut into ~15 % bands at white gaps, each `Table Recognition:` | PaddleOCR-VL, ~3–5 / page |
| 6 | `footer_ocr` | bottom 15 % of the last page, `OCR:` | PaddleOCR-VL, 1–2 calls |
| 7 | `mapping` | `map_invoice` rules (parties, items from table + bands, totals, freight / size-matrix readers) | — (CPU) |
| 8 | `row_reread` | only if item checks fail: the table cut into ~8 % bands, all read again, `map_invoice` again | PaddleOCR-VL, ~6–10 calls |
| 9 | `qwen2b_crosscheck` | header/table/footer OCR text → JSON fields, merged | Qwen3-VL-2B, 1 text call |
| 10 | `region_crops` | `locate_regions` (seller, buyer, invoice details, totals boxes), each block cropped ×2 and read — **always, for every invoice** | Qwen3-VL-2B 1 (+1 multi-page) + PaddleOCR-VL 4 |
| 11 | `field_reread` | up to 8 flagged / failing / missing fields: `locate_field` then a zoomed `OCR:` each | Qwen3-VL-2B + PaddleOCR-VL, 2 calls / field |
| 12 | `qwen8b_fallback` | optional; off in `.env` | Qwen3-VL-8B, 1 / page |
| 13 | `details` | `extract_details` (IRN, LR, bank …) | — (CPU) |
| 14 | `validation` | `validate_invoice` (15 checks), then scoring + routing (`finalize`) and DB storage | — (CPU) |

## 2. Every model call

- PaddleOCR-VL (port 8090): header 1–2, whole page 1/page, bands 3–5/page, footer 1–2,
  row re-read 6–10 when triggered, block crops 4, field re-reads ≤ 8.
- Qwen3-VL-2B (port 8091): table locator 1/page, cross-check 1, block locator 1–2, field locator ≤ 8.
- A single page therefore costs roughly 11–13 PaddleOCR calls and 3 Qwen calls before any re-read,
  up to ~35 calls when rows and 8 fields are re-read.

## 3. Image transformations

PDF render → re-encode → deskew (full-resolution rotate) → per stage: crop (header / footer /
bands / blocks / fields) → zoom up to 2× (blocks, fields) → PNG encode → base64 in the HTTP body.
Table locator: resize to 1,280 px JPEG. All crops are cut from the same deskewed page; the page
itself is sent whole once per `Table Recognition:` call.

## 4. Sequential operations

Everything above runs one after another. Independent pairs that could overlap: the whole-page
table and footer OCR do not depend on the table locator; the 2B cross-check (text) does not
depend on the block crops' OCR. The PaddleOCR client serialises its own calls (`asyncio.Lock`), and
both llama-servers run on one GPU, so only cross-model overlap can help.

## 5. Retries and re-reads

OCR retry on loops (header, footer, blocks, fields); row re-read of the whole table when an item
check fails (not only the failing rows); block crops of all four blocks even when every field in
them is already confirmed; field re-reads of up to 8 fields.

## 6–7. GPU- and CPU-heavy work

GPU: every model call (PaddleOCR-VL image encoding + up to 4,096 output tokens for a table; Qwen
vision encoding). CPU: deskew angle search on full-resolution pages, PNG encoding of crops,
`map_invoice` (called twice when rows are re-read) and validation (called several times inside
the zoom merge and fallback decision, each a full `validate_invoice`).

## 8. Network / IPC

Every call posts a base64 PNG to 127.0.0.1 (a whole 2,560 px page is 1–3 MB of base64); no
network beyond localhost. The API holds one HTTP client per model server.

## 9. Duplicate processing

- The item table is read three times at full detail (whole page, bands, and short bands on a
  failure), and each reading is mapped.
- Blocks are cropped and read even when the header OCR and the 2B cross-check already agree on
  every field in them.

## 10. Main bottlenecks (confirmed by the baseline profile, `benchmarks/performance/baseline.json`)

| Stage | Avg per invoice | Share |
| --- | --- | --- |
| Whole-page table OCR | 13.6 s | 29 % |
| Zoomed block crops (always 4 blocks) | 10.8 s | 23 % |
| Item-band OCR | 10.3 s | 22 % |
| Header OCR | 3.7 s | 8 % |
| Qwen3-VL-2B cross-check | 3.4 s | 7 % |
| Table locator | 1.8 s | 4 % |
| Field re-reads | 1.2 s | 3 % |
| Row re-read (1 of 14 invoices) | 1.0 s | 2 % |
| Deskew, rules, validation, details | 0.2 s | < 1 % |

1. PaddleOCR output tokens: 3,400–9,800 generated per invoice at ~166 tokens/s; the item rows are
   read twice (whole page and bands).
2. Always-on block crops: 5 calls even when every field in the blocks already checks out.
3. Strictly sequential calls on two model servers and one PaddleOCR request at a time, with the
   GPU far from saturated by these small models.

What was done about each is in `PERFORMANCE_REPORT.md`.
