# EXAM — Architecture

A standalone AI answer-sheet correction module. It reads handwritten answer sheets with
**our own Vision AI** (default) or **Claude Vision** (optional, explicit), marks each answer
against a **teacher-defined, versioned rubric** with **Claude** (or, experimentally, our
local LLM), validates every suggested mark in code, and keeps **teachers in control** of
final marks.

```
 Browser (static/app.js)  ──HTTP──►  FastAPI app (port 8004)
                                       │
         ┌─────────────────────────────┼───────────────────────────────┐
         │ routers/                    │ services/                     │ providers/
         │  auth (users, students)     │  documents  (PDF→pages,       │  base       (shared result
         │  settings (AI config)       │              quality, redact, │              + interface)
         │  exams (questions, rubrics, │              blank detection) │  own_vision (our Vision API,
         │         results workflow)   │  segmentation (answer per Q)  │              /v1/vision "OCR:")
         │  submissions (upload, read, │  ocr_guard (hallucination     │  claude_vision (Anthropic API)
         │         compare, review,    │              removal)         │  evaluators (Claude /
         │         marks, approval)    │  prompts   (versioned)        │              local LLM)
         │  reports (report, CSV,      │  scoring   (deterministic     │  claude_common (structured
         │         audit)              │              validation)      │              output, errors)
         │                             │  processing (extract, mark)   │
         │                             │  jobs      (background)       │
         │                             │  reports   (diff, stats)      │
         └─────────────────────────────┴───────────────────────────────┘
                                       │
                         SQLite (data/exam.db) + page images (data/sheets/<id>/)
```

## The six responsibilities (kept separate)

| Responsibility | Where | Notes |
|---|---|---|
| Document processing | `services/documents.py` | PDF/JPEG/PNG detected from content; PDFs rendered at 200 dpi; EXIF rotation; manual 90° rotation; page order; quality checks (dark page, faint writing, low resolution); originals kept unchanged. |
| Vision provider | `providers/own_vision.py`, `providers/claude_vision.py` | Same interface (`extract_page`, `health_check`, `model`) and the same normalised result (`PageExtraction`). Confidence and coordinates are `null` when a provider does not supply them. |
| Contextual evaluation | `providers/evaluators.py`, `services/prompts.py` | One answer per call; rubric first, student answer last inside `<student_answer>` and declared untrusted; structured JSON output whose criterion ids are restricted to the rubric's. |
| Scoring & validation | `services/scoring.py` | Independent of the model: criterion ids, ranges, increments, totals (recomputed), evidence present and found in the answer, numerical final answer and written arithmetic checked by code. |
| Teacher review | `routers/submissions.py`, `static/app.js` | Image beside editable transcription, AI suggestion (clearly labelled), criterion marks, overrides with reasons, approval, reopen, history. |
| Reporting | `services/reports.py`, `routers/reports.py` | Totals computed on the backend; question-wise stats, misconceptions, AI-vs-teacher agreement, overrides, OCR corrections, processing time and cost. |

## Providers and settings

* **Reading** (`vision_provider`: `own` | `claude`) and **marking** (`evaluation_provider`: `claude` | `own_llm`) are separate settings, with a global default (Settings → AI Configuration), a per-exam override, and a per-job choice (buttons on the review screen).
* **No silent switching.** A failed reading marks the sheet *needs attention*, keeps the pages and completed work, and offers *Read again* or *Read with Claude Vision*. Automatic fallback exists only as an administrator setting, requires the external-processing acknowledgement, and every fallback is written to the audit log and shown on the sheet.
* **External processing disclosure.** Claude Vision cannot run until an administrator acknowledges sending student pages to Anthropic. The top of page 1 (name, roll number) is blanked before sending (configurable %).
* **Comparison mode** reads one sheet with both providers and shows word-level differences, number differences, time and cost. Nothing is merged; the teacher picks which reading to use.

## Our Vision AI: how pages are read

1. Each page is cut into horizontal bands at blank rows (default 2) so each band is read at higher resolution.
2. Bands with no ink are **skipped** — on blank ruled paper the model otherwise invents looping text.
3. Each band is read with PaddleOCR-VL's `OCR:` task through the Vision API (`/v1/vision`).
4. `ocr_guard` removes looping / out-of-script output and reports what it removed as a warning.
5. `segmentation` splits the text into answers by question markers (Q1, 1., 1), Ans 2, 3(a) …), skipping header lines repeated on later pages; unknown question numbers are kept and reported, not dropped.

## Data model (append-only where it matters)

`users`, `auth_sessions`, `students`, `exams`, `exam_teachers`, `questions`,
`rubric_versions` (never edited; a change is a new version), `submissions`, `pages`,
`extractions` (every reading kept; `submissions.active_extraction_id` selects one),
`answers` (`original_text` never overwritten; `corrected_text` holds the teacher's version),
`evaluations` (every attempt kept with rubric version, prompt version, provider, model,
usage), `mark_overrides`, `jobs`, `audit_logs`, `settings` (secrets encrypted with Fernet).

## Security

* Passwords: PBKDF2-SHA256 (240k iterations). Sessions: random tokens stored hashed, 12 h.
* Every API checks role and exam assignment on the server; unassigned teachers get 404.
* API keys are encrypted at rest (`data/secret.key`), masked in the UI, never logged, never returned.
* Failed sign-ins are rate-limited (10 per 5 minutes per client).
* Student answers are rendered as text only (no HTML injection); audit entries never contain answer text or keys.
