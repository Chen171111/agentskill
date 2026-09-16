@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1
cd /d "%~dp0"
set LOG=state\daily_run.log
echo ==================== %date% %time% ==================== >> "%LOG%"

rem ---------------------------------------------------------------
rem  Step 1/2  refresh market data + DATA HEALTH CHECK (64-bit Python)
rem  (32-bit Python has NO akshare so it cannot download quotes;
rem   the THS client however must be driven by 32-bit Python)
rem  Exit codes: 0=ok  1=refresh failed  3=health check FAILED
rem  (health check writes state/DATA_ALERT.txt - see tools/refresh_data.py)
rem ---------------------------------------------------------------
"E:\Python\python.exe" -u tools\refresh_data.py >> "%LOG%" 2>&1
if errorlevel 1 echo [WARN] data refresh/health-check FAILED (see state/DATA_ALERT.txt). If the data is stale the trading step will refuse to run due to the staleness guard. >> "%LOG%"

rem ---------------------------------------------------------------
rem  Step 2/2  run one trading session (32-bit Python)
rem  daily_job.py wraps main.py: records state/last_run.json and writes
rem  state/ALERT.txt on failure, so an unattended failure is visible
rem  instead of silently rotting in the log.
rem ---------------------------------------------------------------
"E:\Python32\python.exe" -u tools\daily_job.py >> "%LOG%" 2>&1
echo [done] exit=%errorlevel% >> "%LOG%"
