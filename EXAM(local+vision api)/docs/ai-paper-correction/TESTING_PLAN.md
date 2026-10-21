# EXAM — Testing plan and results

## Automated tests (`backend/tests`, 46 tests, all passing)

Run: `cd backend` then `.venv\Scripts\python.exe -m pytest tests -q`

AI providers are replaced by deterministic fakes, so tests need no GPU, network or Claude key.

| Area | Tests |
|---|---|
| Segmentation | many marker styles (Q1., Ans 2), 3(a)), numbered lists inside an answer, missing and unknown questions, out-of-order answers, headers repeated on later pages |
| Scoring | valid marks; marks over a criterion maximum, wrong increments, negatives, marks without evidence, unknown/missing criteria rejected; evidence not found in the answer → review; empty answer; uncertain OCR → review; numeric final answer and arithmetic slips checked by code; model review requests kept |
| Prompt safety | rubric precedes the fenced, untrusted student answer; criterion ids restricted by schema |
| Documents | PDF rendering and page limit, unsupported files, quality warnings, redaction, band cutting between lines, blank ruled paper vs a single short line (incl. pencil) |
| Hallucination guard | looping output removed and reported; stray foreign script reported; normal text untouched |
| Workflow | upload → read → mark → identity → override (reason required, recorded) → approve → results approved → published → student sees only published results; approved marks locked |
| Permissions | unassigned teacher (404 everywhere incl. page images), student blocked from staff APIs, only admins see AI settings, teachers cannot create exams or publish |
| Secrets | keys never returned, encrypted at rest, absent from audit details |
| Providers | failure → needs attention, no silent switch, explicit Claude retry; auto-fallback only when enabled + acknowledged and audited; Claude Vision blocked without acknowledgement; comparison keeps both readings and merges nothing |
| Robustness | invalid AI output retried once then left to the teacher; missing rubric flagged, model not called; rubric change never touches approved marks; duplicates (same student / same file) rejected; wrong class rejected; bulk upload matching; blank answer = 0 without a model call; marking unavailable without a key while reading still works; first marks are not counted as overrides |

## Live checks done (2026-10-10)

* Our Vision AI (PaddleOCR-VL on the RTX A1000) on the 3 demo sheets — `tools/benchmark_providers.py`:
  **word error rate 0.0, question mapping 100 %, numbers 100 %, 2.2 s per sheet** (after fixes below).
* Found and fixed during live testing:
  - the model invents looping Chinese text on blank ruled paper → blank areas skipped + output guard (43.6 s → 2.2 s per sheet as well);
  - the whole-page formula task loops on handwriting → removed;
  - page-2 headers were attached to the previous answer → header lines skipped;
  - clean scans were flagged "low contrast" → contrast measured between ink and paper.
* UI walked through in a browser (scratch copy): first-run admin, exam list, bulk upload, reading, review screen, manual marking and approval (Divya: 4/10, equal to the teacher key), results page, AI Configuration tests (connection + sample extraction), no horizontal scrolling at 375 px.

## Still to do (needs the school)

1. Enter the Claude key → run `tools/benchmark_providers.py --providers own,claude --mark` for reading comparison and marking agreement with the teacher key (`sample-answer-sheets/ground_truth.json`).
2. Benchmark on 20–30 **real** scanned sheets with teacher transcriptions and marks (different handwriting, poor scans, maths, alternative wording, partial and wrong answers).
3. Define acceptance criteria (e.g. ≥ 90 % within half a mark); keep teacher approval mandatory until they are met.
