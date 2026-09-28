@echo off
setlocal

set "YKT_APP_DIR=%~dp0"

powershell.exe -NoLogo -NoProfile -Command "$expected = [IO.Path]::GetFullPath((Join-Path $env:YKT_APP_DIR '.venv\Scripts\python.exe')); $connection = Get-NetTCPConnection -LocalAddress '127.0.0.1' -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1; if (-not $connection) { Write-Host '[OK] YKT Web is not running.'; exit 0 }; $process = Get-CimInstance Win32_Process -Filter ('ProcessId = ' + $connection.OwningProcess); $parent = if ($process) { Get-CimInstance Win32_Process -Filter ('ProcessId = ' + $process.ParentProcessId) } else { $null }; $processMatches = $process -and $process.CommandLine -match 'server\.py'; $launcherMatches = ($process -and [string]::Equals($process.ExecutablePath, $expected, [StringComparison]::OrdinalIgnoreCase)) -or ($parent -and [string]::Equals($parent.ExecutablePath, $expected, [StringComparison]::OrdinalIgnoreCase)); if (-not ($processMatches -and $launcherMatches)) { Write-Error 'Port 8765 is owned by another process. Nothing was stopped.'; exit 2 }; Stop-Process -Id $process.ProcessId -Force -ErrorAction Stop; Start-Sleep -Milliseconds 300; if ($parent -and (Get-Process -Id $parent.ProcessId -ErrorAction SilentlyContinue) -and [string]::Equals($parent.ExecutablePath, $expected, [StringComparison]::OrdinalIgnoreCase)) { Stop-Process -Id $parent.ProcessId -Force -ErrorAction SilentlyContinue }; Write-Host '[OK] YKT Web has stopped.'"
set "YKT_EXIT=%ERRORLEVEL%"

if not "%YKT_EXIT%"=="0" pause
exit /b %YKT_EXIT%
