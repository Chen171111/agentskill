@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1
cd /d "%~dp0"
set LOG=state\daily_learn.log
echo ==================== %date% %time% ==================== >> "%LOG%"

rem ---------------------------------------------------------------
rem  Daily learning loop (M4): data delta -> features -> label
rem  backfill -> monitor.  Records ONLY, never trades.
rem
rem  RED LINE: this loop must NOT change any finalized rule.
rem  See docs/需求规格_自优化闭环模块.md
rem
rem  64-bit Python: step 1 runs append_stock_bars.py which needs
rem  the akshare stack living in E:\Python's user site-packages.
rem ---------------------------------------------------------------
"E:\Python\python.exe" -u tools\daily_learn.py --adj-mode correct >> "%LOG%" 2>&1
echo [done] exit=%errorlevel% >> "%LOG%"
