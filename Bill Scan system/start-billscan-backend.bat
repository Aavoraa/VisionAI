@echo off
title Bill Scan backend - port 8005
cd /d "%~dp0backend"
.venv\Scripts\python.exe -m uvicorn app.main:app --port 8005
