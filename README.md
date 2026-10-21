# VisionAI

Local AI document platform: invoice extraction on the GPU, plus AI paper correction and daily-bill scanning.

| Folder | What it is | Port |
|---|---|---|
| `vision API/vision-api` | Vision API (FastAPI): invoice extraction with PaddleOCR-VL + Qwen3-VL via llama.cpp, API keys, usage, jobs | 8002 |
| `vision API/dashboard` | Dashboard (Next.js) | 3000 |
| `vision API/invoice-tester` | Invoice tester page (ERP-style form) | 3002 |
| `EXAM(local+vision api)` | AI paper correction: our Vision AI reads, Claude marks | 8004 |
| `exam(vision)` | AI paper correction: Claude reads whole sheets and marks (no GPU needed) | 8006 |
| `Bill Scan system` | Daily bills: scan, reconcile, export (reads via the Vision API) | 5174 / 8005 |

Each folder has its own README. `vision API/start-all.bat` starts the platform on Windows.

## Not in this repository

* **Models and binaries** — download the GGUF models into `vision API/models/` and llama.cpp (CUDA build) into `vision API/tools/` (see `vision API/HOW_TO_RUN.txt`).
* **Secrets** — copy each `.env.example` to `.env` and fill in your own keys. The EXAM modules take the Claude key in their UI.
* **Data** — databases, uploaded invoices, answer sheets and bills stay on the machine that created them.
* **Environments** — create each Python `.venv` from `requirements.txt` and run `npm install` for the front ends.
