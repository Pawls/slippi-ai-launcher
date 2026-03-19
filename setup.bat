@echo off
setlocal

echo ============================================
echo   Slippi AI Launcher - Windows Setup
echo ============================================
echo.

:: Check Python is available
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python is not installed or not on PATH.
    echo Install Python 3.10+ from https://www.python.org/downloads/
    echo Make sure to check "Add Python to PATH" during install.
    pause
    exit /b 1
)

:: Create virtual environment
if not exist ".venv" (
    echo Creating virtual environment...
    python -m venv .venv
) else (
    echo Virtual environment already exists, skipping creation.
)

:: Activate and install
echo Activating virtual environment...
call .venv\Scripts\activate.bat

echo Installing dependencies (this may take a few minutes)...
pip install -e .

echo.
echo ============================================
echo   Setup complete!
echo ============================================
echo.
echo To launch the GUI, run:
echo   .venv\Scripts\activate.bat
echo   python LAUNCHER\slippi_launcher.py
echo.
pause
