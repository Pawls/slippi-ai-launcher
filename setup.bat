@echo off
setlocal

echo ============================================
echo   Slippi AI Launcher - Windows Setup
echo ============================================
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python is not installed or not on PATH.
    echo Install Python 3.10+ from https://www.python.org/downloads/
    echo Make sure to check "Add Python to PATH" during install.
    pause
    exit /b 1
)

if not exist ".venv" (
    echo Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo ERROR: Failed to create virtual environment.
        pause
        exit /b 1
    )
) else (
    echo Virtual environment already exists, reusing it.
)

set "VENV_PY=.venv\Scripts\python.exe"

echo Ensuring pip is present and up to date...
"%VENV_PY%" -m ensurepip --upgrade >nul 2>&1
"%VENV_PY%" -m pip install --upgrade pip
if errorlevel 1 (
    echo ERROR: Could not bootstrap pip inside .venv.
    echo Try deleting the .venv folder and running this script again.
    pause
    exit /b 1
)

echo Installing Slippi AI Launcher and all dependencies ^(this may take a few minutes^)...
"%VENV_PY%" -m pip install -e .
if errorlevel 1 (
    echo ERROR: Dependency install failed. See the output above.
    pause
    exit /b 1
)

echo.
echo ============================================
echo   Setup complete!
echo ============================================
echo.
echo To launch the GUI, run:
echo   .venv\Scripts\activate.bat
echo   python launch.py
echo.
pause
