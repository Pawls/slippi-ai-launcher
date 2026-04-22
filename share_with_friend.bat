@echo off
setlocal

:: Launches the LAUNCHER.api backend bound to 0.0.0.0 so a friend on your
:: tailnet (or LAN, if firewall allows) can reach it. Then opens the GUI,
:: which auto-detects the running backend and adopts it.
::
:: Stop the backend by closing the "Slippi-AI Backend (shared)" console window.

set "REPO_DIR=%~dp0"
set "PYTHON_EXE=%REPO_DIR%.venv\Scripts\python.exe"

if not exist "%PYTHON_EXE%" (
    echo Error: venv python not found at %PYTHON_EXE%
    pause
    exit /b 1
)

:: Kill any existing listener on port 8000 (leftover backend from a prior
:: run, or a backend the GUI auto-spawned). Without this, uvicorn dies with
:: winerror 10048. Use PowerShell because netstat parsing in cmd is fragile.
echo Checking for existing process on port 8000...
powershell -NoProfile -Command ^
    "$pids = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique; if ($pids) { Write-Host \"Killing existing PID(s): $pids\"; $pids | ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue } }"

:: Brief pause to let the OS release the socket before we try to bind.
"%SystemRoot%\System32\timeout.exe" /t 1 /nobreak >nul

:: Start the backend in a new visible window so logs are easy to read and
:: closing the window kills it cleanly. Bind 0.0.0.0 to accept Tailscale
:: connections (loopback 127.0.0.1 keeps working too).
start "Slippi-AI Backend (shared)" cmd /k ^
    ""%PYTHON_EXE%" -m LAUNCHER.api --host 0.0.0.0 --port 8000"

:: Give uvicorn a couple seconds to bind before the GUI probes the port.
:: Use the full System32 path because git-bash/msys on PATH can shadow
:: Windows' timeout.exe with GNU coreutils' incompatible version.
"%SystemRoot%\System32\timeout.exe" /t 3 /nobreak >nul

:: Now launch the Tauri GUI (slippi-ai-gui/dev.bat). It'll see port 8000
:: already in use and adopt the running backend instead of spawning its own.
set "GUI_DIR=%REPO_DIR%..\slippi-ai-gui"
if exist "%GUI_DIR%\dev.bat" (
    pushd "%GUI_DIR%"
    call dev.bat
    popd
) else (
    echo GUI launcher not found at %GUI_DIR%\dev.bat
    echo Backend is still running in the other window. Launch the Tauri GUI manually.
    pause
)
