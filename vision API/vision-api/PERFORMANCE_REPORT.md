# Performance report — invoice extraction

Measured on the 14 benchmark invoices (1,466 hand-checked fields printed on the invoices) with
`scripts/performance_benchmark.py`, live model calls on the RTX A1000 (8 GB). Every run's
machine-readable results are in `benchmarks/performance/<run>.json`, and each invoice's full
result in `benchmarks/performance/<run>/`. Accuracy = fields exactly right; latency = wall clock
per invoice, upload to validated result.

## Baseline (`baseline.json`)

| Metric | Value |
| --- | --- |
| Accuracy | 93.7 % (1,466 fields) |
| Average latency | 46.5 s |
| P50 / P95 | 39.6 s / 67.1 s |
| GPU VRAM (peak) | 6,062 MiB |
| Average model calls | 16.4 (PaddleOCR-VL 11.5, Qwen3-VL-2B 4.9) |
| Routes | 2 approved, 9 needs review, 3 reprocess |

Where the time went (average per invoice): whole-page table OCR 13.6 s, zoomed block crops 10.8 s,
item bands 10.3 s, header 3.7 s, 2B cross-check 3.4 s, table locator 1.8 s, field re-reads 1.2 s,
row re-read 1.0 s; deskew, rules, validation together under 0.2 s.

## Optimized (`optimization_02.json`, the configuration now in `.env`)

| Metric | Value |
| --- | --- |
| Accuracy | 93.9 % |
| Average latency | 29.1 s |
| P50 / P95 | 25.8 s / 44.8 s |
| GPU VRAM (peak) | 6,070 MiB |
| Average model calls | 13.6 (PaddleOCR-VL 9.1, Qwen3-VL-2B 4.5) |
| Routes | 4 approved, 9 needs review, 1 reprocess |

## Improvement

| | Baseline | Optimized | Change |
| --- | --- | --- | --- |
| Average latency | 46.5 s | 29.1 s | **−37 %** |
| P50 latency | 39.6 s | 25.8 s | −35 % |
| P95 latency | 67.1 s | 44.8 s | −33 % |
| Accuracy | 93.7 % | 93.9 % | +0.2 points |
| Model calls per invoice | 16.4 | 13.6 | −17 % |
| 8B invocation rate | 0 % (off) | 0 % (off) | 43 % when switched on (optimization_04) |
| GPU memory (peak) | 6,062 MiB | 6,070 MiB | +8 MiB (unchanged) |
| Review rate | 86 % | 71 % | −15 points |

## Optimizations, one at a time

| Run | Change | Accuracy | Avg latency | Decision |
| --- | --- | --- | --- | --- |
| baseline | — | 93.7 % | 46.5 s | — |
| replay | Skip item bands when the whole-page table passes the item checks | 91.8 % (rules replay) | — | Rejected: invoice-005 fell from 87.5 % to 37.5 % |
| replay | Line items from the bands only (whole-page table for the rest) | 92.6 % (rules replay) | — | Rejected: loses the last row of 2 invoices with every check still passing |
| optimization_01 | Zoom only into blocks with a field missing, flagged or failing; 2B table locator and 2B cross-check run while PaddleOCR reads | 93.7 % | 38.2 s | Kept |
| optimization_02 | + 2 PaddleOCR requests in flight (llama-server slots) | 93.9 % | 29.1 s | Kept |
| optimization_03 | + Qwen3-VL-2B context 16k → 8k | 93.4 % | 31.1 s | Rejected: IGST 80 % → 50 %, 5 reprocess (saved 900 MiB) |
| optimization_04 | + targeted 8B fallback on (8B loaded beside the other two models, 8 of 36 layers on the GPU) | 93.7 % | 72.9 s | Rejected on this GPU: VRAM 8,008 of 8,188 MiB slows every invoice (2B locator 2.5 s → 16.6 s); no accuracy gain. Code kept, off in `.env` |

## 8B fallback

The fallback was made targeted (`invoice_fallback.fallback_scope`): an item check that fails, or
no items, asks for every page's items; totals that cannot be reconciled ask only for the last
page's totals block (a short answer, ~14–22 s instead of 2–13 min); a totals-only reading whose
subtotal shows the items do not add up escalates to the items; a line check skipped for want of a
rate (freight consignments) no longer triggers it. Offline, on the saved 8B readings, this keeps
the full hybrid's 93.9 % with 3 full item readings instead of 9.

Live (optimization_04) it triggered on 6 of 14 invoices, all totals-only, none escalated. The
cost is not the calls but the memory: with the 8B model loaded the GPU sits at 8,008 of 8,188 MiB
and every other model call slows down, so average latency rose from 29.1 s to 72.9 s with no
accuracy gain. On an 8 GB GPU the fallback stays off; with 12–16 GB it can be switched on with
`VISION_INVOICE_FALLBACK_LLM_URL`.

## Accuracy by field (exact, printed fields)

| Field | Baseline | Optimized |
| --- | --- | --- |
| Invoice number | 92.9 % | 92.9 % |
| Invoice date | 92.9 % | 92.9 % |
| Seller GSTIN | 85.7 % | 92.9 % |
| Buyer GSTIN | 92.9 % | 92.9 % |
| Seller name | 71.4 % | 71.4 % |
| Buyer name | 71.4 % | 71.4 % |
| Quantity | 98.0 % | 98.0 % |
| Rate | 98.4 % | 98.4 % |
| Amount (taxable value) | 97.5 % | 97.5 % |
| Tax: CGST / SGST / IGST | 100 / 100 / 70 % | 100 / 100 / 80 % |
| Subtotal | 83.3 % | 83.3 % |
| Grand total | 81.8 % | 81.8 % |

## Accuracy by invoice type

| Type | Invoices | Baseline | Optimized |
| --- | --- | --- | --- |
| GST invoice (incl. tax-summary tables) | 12 | 94.9 % | 95.3 % |
| Freight bill | 1 | 100 % | 100 % |
| Garment size matrix | 1 | 78.0 % | 78.0 % |

## What was assessed and not changed

- **Image preprocessing** (Phase 8): deskew, rendering and crops take 0.13 s per invoice (0.3 %);
  PaddleOCR-VL resizes every image to about one megapixel itself, so cropping the background away
  saves no model time. Document-boundary and perspective correction would only matter for accuracy
  on badly framed photos, which the benchmark cannot show; not added.
- **Targeted row re-reads** (Phase 4): rows were re-read on 1 of 14 invoices (the garment bill,
  13 s); the rows' positions are not known from the OCR markup, so a per-row crop would be a guess.
  Left as is: average cost 1.0–1.3 s per invoice.
- **Field re-reads** (Phase 5): already targeted (flagged, failing or missing fields only, ≤ 8);
  1.2 s per invoice.
- **Qwen3-VL-2B cross-check** (Phase 6): kept on every invoice. It is one text call, and it now runs
  while PaddleOCR reads the item bands, so it costs 0.1–0.8 s of wall time; it is also what lets an
  invoice be approved automatically.
- **llama.cpp flags** (Phase 9): `-ngl 99 -fa on -c 16384` kept; the servers already run 4 slots,
  which is what two requests in flight use. A smaller 2B context was rejected (optimization_03).

## Conclusion

The pipeline is 37 % faster on average (46.5 s → 29.1 s, P95 67.1 s → 44.8 s) with accuracy
unchanged or better (93.7 % → 93.9 %), 17 % fewer model calls and the same GPU memory. The gains
come from three reversible settings, all measured on the full benchmark: zooming only into blocks
with a field in doubt, overlapping the two model servers, and two PaddleOCR requests in flight.
Every business rule and validation check is unchanged (720 backend tests, cached benchmark
identical).

Remaining bottleneck: PaddleOCR-VL's output length. The whole-page table (≈ 13.6 s) and the item
bands (≈ 8–16 s) each read the item rows, and reading them only once was measured to lose rows.
Next hardware step: a 12–16 GB GPU (for example an RTX 4000 Ada 20 GB or RTX A4000 16 GB) would keep
the 8B fallback loaded at full speed next to the other two models, which offline analysis puts at
93.9 % accuracy for about 15–20 s extra on the 40 % of invoices that need it.

## How to repeat

```
python -m pytest                                        # 720 tests
python scripts/benchmark_invoices.py --cached            # accuracy of the rules on saved readings
python scripts/performance_benchmark.py --name <run>     # live speed + accuracy → benchmarks/performance/<run>.json
```
