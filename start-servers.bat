@echo off

if "%1"=="backend" (
    cd /d "D:\SISTEM BARU JOSHUA\Clinic-Patient-Management-System"
    call Scripts\activate.bat
    python manage.py runserver 0.0.0.0:8000
    exit
)

if "%1"=="frontend" (
    cd /d "D:\SISTEM BARU JOSHUA\CPMS-Webapp"
    npm run dev -- --host
    exit
)

start "CMS Backend" cmd /k "%~f0" backend
start "CMS Frontend" cmd /k "%~f0" frontend
