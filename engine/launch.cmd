@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONDONTWRITEBYTECODE=1
set "PY="
if exist "D:\python\python\python.exe" set "PY=D:\python\python\python.exe"
if not defined PY (where py >nul 2>nul && set "PY=py -3")
if not defined PY (where python >nul 2>nul && set "PY=python")
if not defined PY (
  echo [A-Quant] Python 3 not found. Please install Python 3.10+ from python.org
  pause
  exit /b 1
)
powershell -NoProfile -Command "if (Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and $_.CommandLine -like '*aq5.server*' }) { exit 1 } else { exit 0 }"
if errorlevel 1 (
  echo [A-Quant] already running
) else (
  if not exist data mkdir data
  start "A-Quant 5.0 - close this window to stop" /min cmd /c "%PY% -m aq5.server >> data\server.log 2>&1"
  timeout /t 3 /nobreak >nul
)
start "" http://127.0.0.1:18950
exit /b 0
