@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1
cd /d "%~dp0"
set LOG=state\weekly_run.log
echo ==================== %date% %time% ==================== >> "%LOG%"

rem ---------------------------------------------------------------
rem  Weekly research-data refresh: dividends / industry / indexes
rem  (ETF quotes are handled by daily_run.bat, NOT here)
rem
rem  Python choice: 64-bit, because the fetch scripts need the
rem  network/requests stack that lives in E:\Python's user
rem  site-packages -- the research venv does not carry it.
rem
rem  Exit codes: 0 = ok   3 = some step failed (see state/WEEKLY_ALERT.txt)
rem ---------------------------------------------------------------
"E:\Python\python.exe" -u tools\weekly_data.py >> "%LOG%" 2>&1
echo [done] exit=%errorlevel% >> "%LOG%"
