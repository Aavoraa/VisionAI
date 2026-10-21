@echo off
rem Bill Scan (Daily Bills): backend on http://localhost:8005, app on http://localhost:5174.
rem Bills are read by the Vision API on port 8002 (start it with "vision API\start-all.bat").
rem Port 8005 because the Warexx ERP uses 8000 and 8001 on this PC.
setlocal
cd /d "%~dp0"
set "SYS=%SystemRoot%\System32"
start "Bill Scan backend - port 8005" /min "%SYS%\cmd.exe" /k ""%~dp0start-billscan-backend.bat""
start "Bill Scan app - port 5174" /min "%SYS%\cmd.exe" /k ""%~dp0start-billscan-frontend.bat""
echo  Bill Scan starting:  http://localhost:5174   (backend http://localhost:8005/docs)
"%SYS%\ping.exe" -n 8 127.0.0.1 >nul
start "" http://localhost:5174
