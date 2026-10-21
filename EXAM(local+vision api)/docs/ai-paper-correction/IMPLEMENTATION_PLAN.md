# EXAM — Implementation plan and status

Decisions taken with the school owner (2026-10-10): standalone module next to the Vision API;
our Vision AI reads, Claude marks; Claude reading only on explicit choice; Claude key entered
in the UI; dummy answer sheets now, real ones uploaded through the UI.

| Phase | Scope | Status |
|---|---|---|
| 1 Inspection | Reuse the Vision API (`/v1/vision`, keys, quotas, GPU models) instead of duplicating OCR | Done |
| 2 Examinations & rubrics | Exams, questions, teacher assignment, versioned rubrics, explicit re-evaluation | Done |
| 3 Vision providers | Common interface, our Vision AI, Claude Vision, AI Configuration, connection + sample tests, comparison mode, no silent switching | Done (Claude paths covered by tests with fakes; live Claude untested until a key is entered) |
| 4 Document processing | Upload (single, multi-file, bulk by admission no.), PDF pages, rotation, order, quality checks, segmentation, uncertainty review | Done |
| 5 Contextual evaluation | Versioned prompt, structured output, deterministic validation, numeric/arithmetic checks, one retry, rubric-missing flags | Done |
| 6 Teacher dashboard | Review screen, edits, overrides with reasons, approvals, reopen, history | Done |
| 7 Reporting & security | Reports, CSV, roles, audit log, encrypted keys, rate limit | Done |
| 8 Benchmark & pilot | Benchmark tool + demo set; real teacher-marked sheets needed | Reading benchmarked on demo sheets; marking and real handwriting pending (needs Claude key and real scans) |

## Known limitations and recommended improvements

1. **Demo sheets are font-rendered, not real handwriting.** The 0% word-error result proves the pipeline, not real-world accuracy. Run `tools/benchmark_providers.py` on 20–30 real, teacher-transcribed sheets before relying on it.
2. **No word coordinates from our Vision AI** (llama.cpp returns text only), so answer regions cannot be highlighted; the review screen jumps to the page the answer came from instead.
3. **Blur is not detected.** No simple measure was reliable on test scans; dark pages and faint writing are detected.
4. **PaddleOCR-VL's formula task is not used**: on whole handwritten pages it loops. Plain OCR read written calculations exactly in the benchmark; complex notation (fractions, roots, matrices) is untested.
5. **Diagrams** are not evaluated visually by our reader; Claude Vision describes them in brackets. Diagram questions should be teacher-marked.
6. **Local LLM marking (Qwen3-VL-2B) is experimental** — every result is sent to review. A 16–24 GB GPU would allow a larger local marker.
7. **Single process, SQLite.** Fine for one school; for several schools move to PostgreSQL and a job queue (the job table already tracks state).
8. **The encryption key sits next to the database** (`data/secret.key`). Back up both; protect the folder; move the key to an OS secret store for production.
9. **Segmentation relies on question markers.** Sheets without "Q1/1./Ans 1" markers need the teacher to move text between answers.
10. **Teacher approval stays mandatory** until the school's own benchmark meets its acceptance criteria.
