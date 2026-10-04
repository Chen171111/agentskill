@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1
cd /d "%~dp0"
rem ---------------------------------------------------------------
rem  双解释器约定（2026-10-04 可用环境变量覆盖，默认本机路径）：
rem    PY64 = 64 位 Python，负责拉数据（需 akshare）
rem    PY32 = 32 位 Python，负责驱动同花顺 UIA（akshare 装不上 32 位，
rem           而同花顺客户端是 32 位进程，UIA 消息级驱动必须同位数）
rem  换机时：set PYTHON64=D://py//python.exe & set PYTHON32=D://py32//python.exe
rem  （或写进系统环境变量，本文件不改）
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
