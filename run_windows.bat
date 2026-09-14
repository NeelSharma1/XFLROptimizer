@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if errorlevel 1 (
    echo Python Launcher "py" was not found. Install Python 3.10 or newer from python.org.
    exit /b 1
)

echo Installing or updating required packages...
py -3 -m pip install -r requirements.txt
if errorlevel 1 (
    echo Dependency installation failed.
    exit /b 1
)

echo Running tests, the requested optimization, and all reports...
py -3 run_default.py %*
exit /b %errorlevel%
