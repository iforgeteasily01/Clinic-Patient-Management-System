@echo off
setlocal enabledelayedexpansion

set ROOT=%~dp0
set BACKEND=%ROOT%Clinic-Patient-Management-System
set FRONTEND=%ROOT%CPMS-Webapp

REM ── Sub-process entry points ────────────────────────────────────────────────
if "%1"=="backend"      goto :run_backend
if "%1"=="frontend"     goto :run_frontend
if "%1"=="reservations" goto :run_reservations

REM ── Main: pull both repos, install deps if needed, then launch ──────────────
echo ============================================
echo  CMS Alpha ^| Startup
echo ============================================

REM ---- Backend repo ----------------------------------------------------------
echo.
echo [1/2] Checking backend for updates...
cd /d "%BACKEND%"
for /f %%i in ('git rev-parse HEAD 2^>nul') do set BACK_BEFORE=%%i
git pull
if errorlevel 1 (
    echo   WARNING: Backend git pull failed. Starting with existing code.
) else (
    for /f %%i in ('git rev-parse HEAD 2^>nul') do set BACK_AFTER=%%i
    if "!BACK_BEFORE!"=="!BACK_AFTER!" (
        echo   Already up to date.
    ) else (
        echo   Updates pulled. Checking requirements.txt...
        git diff !BACK_BEFORE!..!BACK_AFTER! -- requirements.txt | findstr /r "." >nul 2>&1
        if not errorlevel 1 (
            echo   requirements.txt changed - installing dependencies...
            call Scripts\activate.bat
            pip install -r requirements.txt
        )
    )
)

REM ---- Frontend repo ---------------------------------------------------------
echo.
echo [2/2] Checking frontend for updates...
cd /d "%FRONTEND%"
for /f %%i in ('git rev-parse HEAD 2^>nul') do set FRONT_BEFORE=%%i
git pull
if errorlevel 1 (
    echo   WARNING: Frontend git pull failed. Starting with existing code.
) else (
    for /f %%i in ('git rev-parse HEAD 2^>nul') do set FRONT_AFTER=%%i
    if "!FRONT_BEFORE!"=="!FRONT_AFTER!" (
        echo   Already up to date.
    ) else (
        echo   Updates pulled. Checking package.json...
        git diff !FRONT_BEFORE!..!FRONT_AFTER! -- package.json package-lock.json | findstr /r "." >nul 2>&1
        if not errorlevel 1 (
            echo   package.json changed - running npm install...
            npm install
        )
    )
)

REM ---- Launch ----------------------------------------------------------------
echo.
echo Starting servers...
start "CMS Backend"      cmd /k "%~f0" backend
start "CMS Frontend"     cmd /k "%~f0" frontend
start "CMS Reservations" cmd /k "%~f0" reservations
echo Done. Backend, frontend and the reservation poller launched in separate windows.
exit /b 0

REM ── Backend window ──────────────────────────────────────────────────────────
:run_backend
cd /d "%BACKEND%"
call Scripts\activate.bat
echo Running migrations...
python manage.py migrate
if errorlevel 1 (
    echo Migration failed. Press any key to exit.
    pause >nul
    exit /b 1
)
echo Starting Django backend on 0.0.0.0:8000...
python manage.py runserver 0.0.0.0:8000
exit /b

REM ── Frontend window ─────────────────────────────────────────────────────────
:run_frontend
cd /d "%FRONTEND%"
echo Starting Vite dev server...
npm run dev -- --host
exit /b

REM ── Online reservation poller ───────────────────────────────────────────────
REM Vercel cannot reach this machine, so the clinic collects instead: one pass a
REM minute against /api/reservation-sync. Each pass is idempotent, so closing
REM this window loses nothing — the bookings queue up on Vercel and the next
REM run collects the backlog.
:run_reservations
cd /d "%BACKEND%"
call Scriptsctivate.bat
echo Polling for online reservations every 60s...
python manage.py poll_reservations --loop --interval 60
exit /b
