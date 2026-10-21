@echo off
rem Invoice tester on http://localhost:3002, talking to the API on port 8002.
title Vision invoice tester - port 3002
cd /d "%~dp0"
vision-api\.venv\Scripts\python.exe invoice-tester\server.py --api http://127.0.0.1:8002
