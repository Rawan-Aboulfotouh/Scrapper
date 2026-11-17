@echo off
REM Keeps running and scrapes every enabled site on the schedule set for it in config.yaml.
REM Leave this window open, or let scripts\install_windows_task.ps1 start it at logon.
setlocal
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
    echo Virtual environment not found. Follow the installation steps in README.md first.
    exit /b 2
)
set CRAWL4AI_NO_CLOUD_NOTICE=1
".venv\Scripts\python.exe" -m scraper schedule %*
exit /b %ERRORLEVEL%
