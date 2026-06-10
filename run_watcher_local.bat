@echo off
rem ------------------------------------------------------------
rem Unreleased-card watcher (local runner)
rem yu-gi-oh.jp blocks cloud IPs (403), so we crawl from this PC.
rem Scheduled task: CardSouba-UnreleasedWatcher (daily)
rem Secrets: %USERPROFILE%\.cardsouba\watcher.env
rem Log:     %USERPROFILE%\.cardsouba\watcher.log
rem ------------------------------------------------------------
cd /d "%~dp0"

set "ENVFILE=%USERPROFILE%\.cardsouba\watcher.env"
set "LOGFILE=%USERPROFILE%\.cardsouba\watcher.log"

if not exist "%ENVFILE%" (
  echo [%date% %time%] watcher.env not found: %ENVFILE% >> "%LOGFILE%"
  exit /b 1
)

rem load KEY=VALUE pairs (lines starting with # are comments)
for /f "usebackq eol=# tokens=1,* delims==" %%a in ("%ENVFILE%") do set "%%a=%%b"

rem whitelist guard tests - abort crawl if they fail
python -m pytest tests\test_fetch_guard.py -q >> "%LOGFILE%" 2>&1
if errorlevel 1 (
  echo [%date% %time%] tests failed - watcher aborted >> "%LOGFILE%"
  exit /b 1
)

echo [%date% %time%] watcher start >> "%LOGFILE%"
python watch_unreleased.py >> "%LOGFILE%" 2>&1
echo [%date% %time%] watcher end ^(exit %errorlevel%^) >> "%LOGFILE%"
