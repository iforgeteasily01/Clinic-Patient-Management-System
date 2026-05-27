@echo off

if "%1"=="backend" (
    cd /d "D:\SISTEM CUSTOM JOSHUA\Clinic-Patient-Management-System"
    call Scripts\activate.bat
    echo Running migrations...
    python manage.py migrate
    if errorlevel 1 (
        echo Migration failed. Press any key to exit.
        pause >nul
        exit /b 1
    )
    echo Starting backend server...
    python manage.py runserver 0.0.0.0:8000
    exit
)

if "%1"=="frontend" (
    cd /d "D:\SISTEM CUSTOM JOSHUA\CPMS-Webapp"
    npm run dev -- --host
    exit
)

echo Pulling latest updates from git...
cd /d "D:\SISTEM CUSTOM JOSHUA"
git pull
if errorlevel 1 (
    echo Git pull failed. Starting with existing code.
)

start "CMS Backend" cmd /k "%~f0" backend
start "CMS Frontend" cmd /k "%~f0" frontend
