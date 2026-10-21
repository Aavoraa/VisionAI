@echo off
rem EXAM - AI paper correction on http://localhost:8004
rem Reading uses our Vision API on port 8002 (start it with "vision API\start-all.bat").
title EXAM paper correction - port 8004
cd /d "%~dp0backend"
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8004
