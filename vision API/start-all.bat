@echo off
rem Starts every part of the Vision AI system, in order, and shows the local links.
rem Anything already running is left as it is. Each server opens in its own window;
rem close that window to stop it.
setlocal
title Vision AI - start all
cd /d "%~dp0"
set "SYS=%SystemRoot%\System32"
set "HERE=%~dp0"
set "FAILED="
set "BILLSCAN=%~dp0..\Bill Scan system\"
set "EXAMDIR=%~dp0..\EXAM\"

echo.
echo  Vision AI - starting all servers
echo  ================================
echo.

call :start 8090 "OCR model - PaddleOCR-VL, GPU"     start-ocr-gpu.bat       http://127.0.0.1:8090/health 120
call :start 8091 "Reasoning model - Qwen3-VL-2B, GPU" start-reasoner-gpu.bat  http://127.0.0.1:8091/health 120
call :start 8002 "API"                               start-api.bat           http://127.0.0.1:8002/health 90
call :start 3002 "Invoice tester"                    start-tester.bat        http://127.0.0.1:3002/api/health 60
call :start 3000 "Dashboard"                         start-dashboard.bat     http://localhost:3000/login 180
if exist "%BILLSCAN%start-billscan-backend.bat" call :start 8005 "Bill Scan backend"  "..\Bill Scan system\start-billscan-backend.bat"  http://127.0.0.1:8005/docs 60
if exist "%BILLSCAN%start-billscan-frontend.bat" call :start 5174 "Bill Scan app"     "..\Bill Scan system\start-billscan-frontend.bat" http://localhost:5174 90
if exist "%EXAMDIR%start-exam.bat" call :start 8004 "EXAM paper correction" "..\EXAM\start-exam.bat" http://127.0.0.1:8004/api/health 60

echo.
if defined FAILED echo  Some servers did not answer in time - check their windows. Links:
if not defined FAILED echo  All servers are up:
echo  ------------------------------------------------------------
echo    Invoice tester   http://localhost:3002
echo    Dashboard        http://localhost:3000
echo    API              http://localhost:8002      docs: http://localhost:8002/docs
echo    OCR model        http://127.0.0.1:8090
echo    Reasoning model  http://127.0.0.1:8091
echo    EXAM correction  http://localhost:8004
echo    Bill Scan        http://localhost:5174      backend: http://localhost:8005/docs
echo  ------------------------------------------------------------
echo.
start "" http://localhost:3002
echo  The invoice tester is opening in your browser. You can close this window;
echo  the servers keep running in their own windows.
echo.
pause
exit /b 0

rem ---------------------------------------------------------------------------
rem :start <port> <name> <script> <health url> <seconds to wait>
rem (no ( ) blocks here: a name with brackets would break them)
:start
"%SYS%\netstat.exe" -ano | "%SYS%\findstr.exe" /R /C:":%~1 .*LISTENING" >nul
if errorlevel 1 goto launch
echo  [running]  %~2 already on port %~1
exit /b 0
:launch
echo  [start]    %~2 ...
start "%~2" /min "%SYS%\cmd.exe" /k ""%HERE%%~3""
set /a waited=0
:wait
"%SYS%\curl.exe" -s -o nul -m 3 "%~4" >nul 2>&1
if errorlevel 1 goto notyet
echo  [ready]    %~2 on port %~1
exit /b 0
:notyet
if %waited% geq %~5 goto timedout
"%SYS%\ping.exe" -n 3 127.0.0.1 >nul
set /a waited+=2
goto wait
:timedout
echo  [WARNING]  %~2 did not answer on port %~1 after %~5 s - check its window
set "FAILED=1"
exit /b 1
