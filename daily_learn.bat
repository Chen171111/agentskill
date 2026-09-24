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
rem  WHY THE RESEARCH VENV (NOT E:\Python\python.exe):
rem    Step 1 shells out to append_stock_bars.py via sys.executable.
rem    The stock-line fetchers use stdlib urllib (no akshare/requests),
rem    but they MUST write parquet -> they need pyarrow.
rem    E:\Python (3.14) has akshare but NO pyarrow -> step 1 fails
rem    5,457/5,457 with ImportError, and it fails *silently* (the step
rem    is non-blocking by design). Measured 2026-09-21 (quant2 space).
rem ---------------------------------------------------------------
set RPY=C:\Users\XiaoQi\.workbuddy-ai\binaries\python\envs\default\Scripts\python.exe
"%RPY%" -u tools\daily_learn.py --adj-mode correct >> "%LOG%" 2>&1
echo [done] exit=%errorlevel% >> "%LOG%"
