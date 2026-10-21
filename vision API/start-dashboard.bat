@echo off
rem Dashboard (Next.js) on http://localhost:3000, talking to the API on port 8002.
title Vision dashboard - port 3000
cd /d "%~dp0dashboard"
set "VISION_API_URL=http://localhost:8002"
set "NEXT_PUBLIC_API_URL=http://localhost:8002"
npm run dev
