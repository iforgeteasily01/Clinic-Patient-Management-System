@echo off
cd /d "d:\Major Projects\CMS Alpha\cms\Clinic-Patient-Management-System"
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
