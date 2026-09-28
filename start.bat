@echo off
setlocal

set "YKT_APP_DIR=%~dp0"
set "YKT_PYTHON=%YKT_APP_DIR%.venv\Scripts\python.exe"
set "YKT_URL=http://127.0.0.1:8765/"

if not exist "%YKT_PYTHON%" (
    echo [ERROR] Python environment not found:
    echo         %YKT_PYTHON%
    echo Run the local installation steps first.
    pause
    exit /b 1
)

powershell.exe -NoLogo -NoProfile -Command "try { $health = Invoke-RestMethod -Uri ($env:YKT_URL + 'api/health') -TimeoutSec 2; if ($health.ok) { exit 0 } } catch {}; exit 1"
if not errorlevel 1 (
    echo [OK] YKT Web is already running at %YKT_URL%
    if not defined YKT_NO_BROWSER start "" "%YKT_URL%"
    exit /b 0
)

echo Starting YKT Web in the background...
powershell.exe -NoLogo -NoProfile -Command "$appDir = $env:YKT_APP_DIR; Start-Process -FilePath (Join-Path $appDir '.venv\Scripts\python.exe') -ArgumentList 'server.py' -WorkingDirectory $appDir -WindowStyle Hidden"
if errorlevel 1 (
    echo [ERROR] Failed to create the background process.
    pause
    exit /b 1
)

powershell.exe -NoLogo -NoProfile -Command "$deadline = (Get-Date).AddSeconds(15); do { try { $health = Invoke-RestMethod -Uri ($env:YKT_URL + 'api/health') -TimeoutSec 2; if ($health.ok) { exit 0 } } catch {}; Start-Sleep -Milliseconds 300 } while ((Get-Date) -lt $deadline); exit 1"
if errorlevel 1 (
    echo [ERROR] YKT Web did not become ready. Port 8765 may be in use.
    pause
    exit /b 1
)

echo [OK] YKT Web is running at %YKT_URL%
if not defined YKT_NO_BROWSER start "" "%YKT_URL%"
exit /b 0
