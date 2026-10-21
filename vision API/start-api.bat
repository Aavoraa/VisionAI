@echo off
rem Vision API (FastAPI) on http://localhost:8002, SQLite database, no Redis.
rem Port 8002 because the Warexx ERP uses 8000 and 8001 on this PC.
title Vision API - port 8002
cd /d "%~dp0vision-api"
set "VISION_DATABASE_URL=sqlite+aiosqlite:///./dev.db"
set "VISION_REDIS_SOCKET_TIMEOUT=0.2"
.venv\Scripts\python.exe -m uvicorn app.main:create_app --factory --port 8002
