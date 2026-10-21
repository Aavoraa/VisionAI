# EXAM — AI Paper Correction

Reads handwritten answer sheets with our own Vision AI, marks them against teacher rubrics
with Claude, and keeps teachers in control of every final mark.

## Start

1. Start the Vision AI (models + API on 8002): `vision API\start-all.bat` — it now also starts EXAM.
   Or start EXAM alone: `EXAM\start-exam.bat`.
2. Open **http://localhost:8004**.
3. First visit: create the administrator account.
4. Settings → AI Configuration:
   * Our Vision AI is pre-configured (URL `http://127.0.0.1:8002`, key "EXAM paper correction"). Click **Test connection** / **Sample extraction**.
   * Paste your **Claude API key**, tick **Enabled**, Save, then **Test marking model**.
   * Optional: to allow Claude Vision reading, tick the external-processing acknowledgement.
5. Add teachers (Settings → Users) and assign them to exams (exam → Exam settings).

## Try the demo

A demo exam *Unit Test 1 — Science (demo)* (Class 7 A, 4 questions, rubrics included) and
three students are already loaded. Dummy handwritten sheets are in `sample-answer-sheets/`:

* Answer sheets tab → **Bulk upload** → choose `A1001.pdf`, `A1002.pdf`, `A1003.pdf`.
* Open a sheet → confirm the student → check the transcription → review the AI suggestion → approve.
* `ground_truth.json` has the exact text and the teacher's expected marks
  (Arun 10, Divya 4, Kiran 5.5 out of 10).

## Folders

| Path | |
|---|---|
| `backend/app` | FastAPI application (see `docs/ai-paper-correction/ARCHITECTURE.md`) |
| `backend/static` | the web UI |
| `backend/tests` | automated tests (`.venv\Scripts\python.exe -m pytest tests -q`) |
| `backend/tools` | `make_dummy_sheets`, `seed_demo`, `benchmark_providers` |
| `backend/data` | database, page images, encryption key — **back this folder up; keep it private** |
| `sample-answer-sheets` | demo sheets + ground truth |
| `benchmarks` | benchmark results (JSON) |
| `docs/ai-paper-correction` | architecture, API, plan, testing |

## Benchmark

```
cd backend
.venv\Scripts\python.exe -m tools.benchmark_providers --providers own,claude --mark
```
