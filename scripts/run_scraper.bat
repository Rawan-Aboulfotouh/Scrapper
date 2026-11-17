@echo off
REM Runs the scraper once (double-click for a manual run).
REM Any extra arguments are passed through, e.g.:  run_scraper.bat --site noon --limit 5
setlocal
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
    echo Virtual environment not found. Follow the installation steps in README.md first.
    exit /b 2
)
set CRAWL4AI_NO_CLOUD_NOTICE=1
".venv\Scripts\python.exe" -m scraper run %*
exit /b %ERRORLEVEL%
