# backend/app/main.py
"""FastAPI application entry point."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .models import init_db
from .routers import bills

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    logging.getLogger(__name__).info(
        "Daily Bills ready. extractor=%s model=%s db=%s",
        settings.extractor,
        settings.vision_api_url if settings.extractor != "claude" else settings.model,
        settings.db_path,
    )
    yield


app = FastAPI(
    title="Daily Bills",
    description="Scan a daily bill, reconcile its arithmetic, review, confirm.",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(bills.router)


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "model": settings.model}
