# EXAM Vision — Plan and status

Approved 2026-10-14 ("approve all defaults"): new folder exam-vision, Claude-only (no local
vision dependency), port 8006, Claude key and acknowledgement copied from EXAM, Opus 5.5 by
default, separate launcher (not in start-all.bat), live benchmark run.

| Step | Status |
|---|---|
| Copy EXAM without data/venv; own venv and database | Done |
| Remove local reader, local LLM, band/blank/guard code, segmentation, comparison, fallback | Done |
| Whole-sheet Claude reading with question mapping, name check, unassigned writing | Done |
| Claude-only AI Configuration with model choice (Opus 5.5 / Sonnet 5.5) | Done |
| Per-sheet usage and cost | Done |
| Tests (37) | Done |
| Live benchmark | Done — see TESTING_PLAN.md |
| Docs, launcher | Done |

## Known limitations

1. Needs internet and Anthropic availability; a failed reading leaves the sheet "needs attention" for a retry.
2. Every page is sent to Anthropic — the school's consent arrangements must cover it.
3. No OCR confidence or word coordinates (Claude reports none); answers jump to their page instead.
4. Very long sheets: up to 40 pages per sheet in one request; output is capped at 32k tokens.
5. Blur is not detected; dark pages and faint writing are.
6. Demo results come from font-rendered sheets; real-handwriting accuracy must be measured on the school's own sheets.
7. SQLite, single process: fine for one school.
