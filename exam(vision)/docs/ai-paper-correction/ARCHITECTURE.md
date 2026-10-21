# EXAM Vision — Architecture

Standalone module (port 8006). Claude reads and marks; the backend validates, stores and
enforces permissions; teachers approve. No dependency on local models, GPU or the Vision API.

```
 Browser (static/app.js) ──HTTP──► FastAPI (port 8006) ──HTTPS──► Anthropic API (Claude)
                                     │
   routers/  auth · settings · exams · submissions · reports
   services/ documents (PDF→pages, quality, redaction) · processing (read, map, mark)
             prompts (versioned) · scoring (deterministic checks) · rubric_draft · jobs · reports
   providers/ claude_vision (ClaudeReader: whole sheet) · evaluators (ClaudeEvaluator)
              claude_common (structured output, streaming, refusal fallback, errors, prices)
                                     │
                       SQLite data/exam.db + page images data/sheets/<id>/
```

## Reading a sheet (`providers/claude_vision.py`)

One streamed Messages request per sheet: every page as a JPEG (≤ 2000 px) labelled
"Page n of N", followed by the exam's question list. A JSON schema (structured output) fixes
the response shape and restricts `question_number` to the exam's numbers. Returned:
page-by-page transcription, answers matched to questions (text, pages, uncertain words),
`unassigned_text` (writing under no question), `name_on_sheet`, warnings. Doubtful words are
marked `[?word?]`, unreadable ones `[illegible]`; such answers must be verified by a teacher
before they are marked. Page numbers outside the sheet are dropped; a question answered twice
keeps both parts with a warning.

## Marking

Unchanged from EXAM: one request per answer, rubric first, the student's answer fenced and
declared untrusted, criterion ids restricted by schema, then `services/scoring.py` validates
ids, ranges, steps, totals, evidence presence and location, the numeric final answer and
written arithmetic. One retry on invalid output, then the answer goes to the teacher.

## Privacy

Every sheet goes to Anthropic, so reading is blocked until an administrator acknowledges
external processing (the school's consent duties, incl. parental consent for children's data).
Blanking the top of page 1 is optional (off by default, because the name is used for the
identity check). The key is encrypted at rest and never returned; audit entries hold no answer
text or keys.

## What changed from EXAM

Removed: our Vision AI reader, local-LLM marker, band splitting, blank detection, hallucination
guard, regex segmentation, comparison mode, automatic fallback, per-exam provider overrides.
Added: whole-sheet reading with Claude's question mapping, name check, unassigned writing,
per-sheet cost, model choice (Opus 5.5 / Sonnet 5.5) for reading and marking separately.
