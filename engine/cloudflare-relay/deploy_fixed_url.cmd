@echo off
chcp 65001 >nul
rem 部署手机远程的固定地址（只需运行一次）
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy.ps1"
pause
