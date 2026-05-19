@echo off
start "CMS Backend" cmd /k "cd /d "D:\SISTEM BARU JOSHUA\Clinic-Patient-Management-System" && call Scripts\activate.bat && python manage.py runserver 0.0.0.0:8000"
start "CMS Frontend" cmd /k "cd /d "D:\SISTEM BARU JOSHUA\CPMS-Webapp" && npm run dev -- --host"
