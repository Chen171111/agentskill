@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1
cd /d "%~dp0"
rem ---------------------------------------------------------------
rem  [!!] THIS FILE MUST STAY PURE ASCII. DO NOT WRITE CHINESE HERE.
rem  Why (2026-10-07, root-caused): this file runs "chcp 65001" above, but
rem  cmd.exe still reads its own batch bytes with a drifting file pointer.
rem  Any non-ASCII byte in this file desynchronises the parser, which then
rem  silently mangles LATER lines. A Chinese comment block added on 10-04
rem  did exactly that: the PY32 call at the bottom got corrupted, cmd
rem  printed "The system cannot find the path specified." and the whole
rem  trading step never ran (exit=3) on 10-06 / 10-07.
rem  Verified: same script with ASCII-only comments runs both steps, exit=0.
rem ---------------------------------------------------------------
rem  Dual-interpreter convention (override via env vars, default = this machine):
rem    PY64 = 64-bit Python, fetches market data (needs akshare)
rem    PY32 = 32-bit Python, drives the THS client via UIA (akshare has no
rem           32-bit build; the THS client is a 32-bit process, so UIA
rem           message-level driving must match bitness)
rem  To port: set PYTHON64=D:\py\python.exe & set PYTHON32=D:\py32\python.exe
rem  (or set them as system env vars; this file stays untouched)
rem ---------------------------------------------------------------
if "%PYTHON64%"=="" set "PY64=E:\Python\python.exe"
if not "%PYTHON64%"=="" set "PY64=%PYTHON64%"
if "%PYTHON32%"=="" set "PY32=E:\Python32\python.exe"
if not "%PYTHON32%"=="" set "PY32=%PYTHON32%"
set LOG=state\daily_run.log
echo ==================== %date% %time% ==================== >> "%LOG%"

rem ---------------------------------------------------------------
rem  Step 1/2  refresh market data + DATA HEALTH CHECK (64-bit Python)
rem  (32-bit Python has NO akshare so it cannot download quotes;
rem   the THS client however must be driven by 32-bit Python)
rem  Exit codes: 0=ok  1=refresh failed  3=health check FAILED
rem  (health check writes state/DATA_ALERT.txt - see tools/refresh_data.py)
rem ---------------------------------------------------------------
"%PY64%" -u tools\refresh_data.py >> "%LOG%" 2>&1
if errorlevel 1 echo [WARN] data refresh/health-check FAILED (see state/DATA_ALERT.txt). If the data is stale the trading step will refuse to run due to the staleness guard. >> "%LOG%"

rem ---------------------------------------------------------------
rem  Step 2/2  run one trading session (32-bit Python)
rem  daily_job.py wraps main.py: records state/last_run.json and writes
rem  state/ALERT.txt on failure, so an unattended failure is visible
rem  instead of silently rotting in the log.
rem
rem  [!] MANUAL / DEBUG RERUNS -> add --manual :
rem        "%PY32%" -u tools\daily_job.py --manual
rem      It writes state/last_run_manual.json + state/ALERT_manual.txt and
rem      will NOT overwrite last_run.json, so a failed extra run can no longer
rem      make the next check_alerts.py report a false "auto-trade FAILED"
rem      (that is exactly what happened on 2026-09-18 21:02).
rem ---------------------------------------------------------------
"%PY32%" -u tools\daily_job.py >> "%LOG%" 2>&1
echo [done] exit=%errorlevel% >> "%LOG%"
