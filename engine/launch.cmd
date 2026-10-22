@echo off
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONDONTWRITEBYTECODE=1
if not exist data mkdir data
echo [%date% %time%] launch.cmd start > data\launch.log
set "PY="
if exist "D:\python\python\python.exe" set "PY=D:\python\python\python.exe"
if not defined PY (where py >nul 2>nul && set "PY=py -3")
if not defined PY (where python >nul 2>nul && set "PY=python")
if not defined PY (
  echo [A-Quant] Python 3 not found. Please install Python 3.10+ from python.org
  echo python not found >> data\launch.log
  pause
  exit /b 1
)
echo PY=%PY% >> data\launch.log
rem If it is already running, the new process exits by itself (port in use) and the log says so.
start "A-Quant 5.0 - close this window to stop" /min cmd /c "%PY% -m aq5.server >> data\server.log 2>&1"
echo started >> data\launch.log
timeout /t 4 /nobreak >nul
start "" http://127.0.0.1:18950
exit /b 0
