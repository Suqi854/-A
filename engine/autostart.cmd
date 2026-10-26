@echo off
rem A-Quant 5.0 auto start at Windows logon: starts the engine minimized, does NOT open the browser.
rem Startup shortcut: %APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\A-Quant 5.0.lnk (delete it to turn off).
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONDONTWRITEBYTECODE=1
if not exist data mkdir data
echo [%date% %time%] autostart.cmd start >> data\launch.log
rem Wait for network after boot; if the engine is already running, the new process exits by itself (port in use).
timeout /t 20 /nobreak >nul
set "PY="
if exist "D:\python\python\python.exe" set "PY=D:\python\python\python.exe"
if not defined PY (where py >nul 2>nul && set "PY=py -3")
if not defined PY (where python >nul 2>nul && set "PY=python")
if not defined PY (
  echo python not found >> data\launch.log
  exit /b 1
)
start "A-Quant 5.0 - close this window to stop" /min cmd /c "%PY% -m aq5.server >> data\server.log 2>&1"
echo autostart started >> data\launch.log
exit /b 0
