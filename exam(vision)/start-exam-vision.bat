@echo off
rem EXAM Vision - AI paper correction with Claude, on http://localhost:8006
rem Needs only internet access to Claude; no local GPU models or Vision API.
title EXAM Vision - port 8006
cd /d "%~dp0backend"
start "" http://localhost:8006
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8006
