@echo off
chcp 65001 >nul
rem Set up reliable cloud-guard start (run once): stores a GitHub token as Worker secret GH_TOKEN and deploys the worker cron.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0cloudflare-relay\setup_cloud_kick.ps1"
pause

