# backend/app/main.py
"""EXAM — AI paper correction. FastAPI app serving the API and the single-page UI."""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import db
from .config import BASE_DIR, settings
from .routers import auth, exams, reports, settings as settings_router, submissions
from .services import jobs

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
STATIC = BASE_DIR / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.configure(settings)
    if settings.run_jobs:
        jobs.recover()
    yield


app = FastAPI(title="EXAM — AI Paper Correction", version="1.0.0", lifespan=lifespan,
              docs_url="/api/docs", openapi_url="/api/openapi.json")

# Failed sign-ins per client: at most 10 per 5 minutes.
_failures: dict[str, deque] = defaultdict(deque)


@app.middleware("http")
async def guard(request: Request, call_next):
    window = None
    if request.url.path == "/api/auth/login" and request.method == "POST":
        window = _failures[request.client.host if request.client else "?"]
        cutoff = time.monotonic() - 300
        while window and window[0] < cutoff:
            window.popleft()
        if len(window) >= 10:
            return JSONResponse({"detail": "Too many failed sign-ins. Wait a few minutes."}, status_code=429)
    response = await call_next(request)
    if window is not None and response.status_code == 401:
        window.append(time.monotonic())
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    if request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


for r in (auth.router, settings_router.router, exams.router, submissions.router, reports.router):
    app.include_router(r)


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")
