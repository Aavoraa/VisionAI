@echo off
setlocal
title Daily Bills - Launcher
cd /d "%~dp0"

echo ============================================
echo   Daily Bills - starting backend + frontend
echo ============================================
echo.

REM ---- Prerequisites --------------------------------------------------------
where python >nul 2>&1 || (echo [ERROR] Python 3.11+ not found on PATH. & goto :fail)
where npm    >nul 2>&1 || (echo [ERROR] Node.js 18+ / npm not found on PATH. & goto :fail)

REM ---- Backend setup --------------------------------------------------------
pushd backend

if not exist ".venv\Scripts\python.exe" (
    echo [backend] Creating virtual environment...
    python -m venv .venv || (popd & goto :fail)
    echo [backend] Installing requirements...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt || (popd & goto :fail)
)

if not exist ".env" (
    copy ".env.example" ".env" >nul
    echo.
    echo [backend] Created backend\.env from .env.example.
    echo           Put your ANTHROPIC_API_KEY in it, then run this file again.
    echo           ^(Skip this if you use an `ant auth login` profile.^)
    echo.
    notepad ".env"
)

popd

REM ---- Frontend setup -------------------------------------------------------
pushd frontend
if not exist "node_modules" (
    echo [frontend] Installing npm packages...
    call npm install || (popd & goto :fail)
)
popd

REM ---- Launch ---------------------------------------------------------------
echo.
echo Starting backend  on http://127.0.0.1:8000 ...
start "Daily Bills - Backend" cmd /k "cd /d "%~dp0backend" && .venv\Scripts\python.exe -m uvicorn app.main:app --reload --env-file .env"

echo Starting frontend on http://localhost:5173 ...
start "Daily Bills - Frontend" cmd /k "cd /d "%~dp0frontend" && npm run dev"

REM Give the dev servers a few seconds, then open the app.
timeout /t 5 /nobreak >nul
start "" "http://localhost:5173"

echo.
echo Both servers are running in their own windows. Close those windows to stop.
endlocal
exit /b 0

:fail
echo.
echo Setup failed. See the messages above.
pause
endlocal
exit /b 1
