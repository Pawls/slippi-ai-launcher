@echo off
:: Target the python.exe in the parent directory's .venv
set "PYTHON_EXE=%~dp0..\.venv\Scripts\python.exe"

if exist "%PYTHON_EXE%" (
    "%PYTHON_EXE%" "%~dp0slippi_launcher.py"
) else (
    echo Error: Virtual environment not found at %PYTHON_EXE%
    pause
)