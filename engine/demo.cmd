@echo off
rem Offline demo with simulated market data (separate data folder, port 18951)
chcp 65001 >nul
cd /d "%~dp0"
set AQ5_MOCK=1
set "AQ5_DATA=%~dp0data_demo"
set "PY=python"
if exist "D:\python\python\python.exe" set "PY=D:\python\python\python.exe"
start "A-Quant DEMO" /min "%PY%" -m aq5.server 18951
timeout /t 3 /nobreak >nul
start "" http://127.0.0.1:18951
