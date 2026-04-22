@echo off
setlocal

:: One-time setup: adds Windows Firewall rules so the LAUNCHER.api backend
:: (port 8000) is reachable from your Tailscale tailnet but blocked from
:: every other network. Loopback (127.0.0.1) always works regardless.
::
:: Run this ONCE. Re-running is safe — it removes any previous copies of
:: these rules first so you don't accumulate duplicates.
::
:: Requires admin. The script self-elevates via UAC if needed.

:: ── Self-elevate to admin ──────────────────────────────────────────────
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo Requesting admin rights...
    powershell -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

echo.
echo Configuring Windows Firewall for Slippi-AI Launcher + Tailscale sharing
echo ========================================================================
echo.

:: ── Remove any prior copies (idempotent) ───────────────────────────────
:: Also removes the legacy "block other" rule from earlier versions of this
:: script — in Windows Firewall, Block rules take priority over Allow rules
:: even when the Allow is more specific, so that block was silently
:: shadowing the Tailscale allow and breaking remote connections.
powershell -NoProfile -Command ^
    "Get-NetFirewallRule -DisplayName 'Slippi-AI Launcher (Tailscale only)' -ErrorAction SilentlyContinue | Remove-NetFirewallRule"
powershell -NoProfile -Command ^
    "Get-NetFirewallRule -DisplayName 'Slippi-AI Launcher (block other)' -ErrorAction SilentlyContinue | Remove-NetFirewallRule"

:: ── Allow inbound from the Tailscale subnet (100.64.0.0/10) ───────────
:: Windows Firewall default-denies inbound, so a single Allow rule scoped
:: to the tailnet subnet is sufficient — no explicit Block rule needed.
echo Adding Allow rule for tailnet traffic on TCP 8000...
powershell -NoProfile -Command ^
    "New-NetFirewallRule -DisplayName 'Slippi-AI Launcher (Tailscale only)' -Direction Inbound -Protocol TCP -LocalPort 8000 -RemoteAddress 100.64.0.0/10 -Action Allow | Out-Null"
if %errorLevel% neq 0 (
    echo Failed to add Allow rule.
    pause
    exit /b 1
)

echo.
echo Done. Port 8000 is now reachable from your tailnet only.
echo.
echo To verify: get-netfirewallrule -DisplayName 'Slippi-AI Launcher*' in PowerShell.
echo To undo:   re-run this script's removal lines, or use Windows Defender Firewall UI.
echo.
pause
