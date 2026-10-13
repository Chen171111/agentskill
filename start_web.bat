@echo off
rem ---------------------------------------------------------------
rem  [!!] THIS FILE MUST STAY PURE ASCII. DO NOT WRITE CHINESE HERE.
rem  (Same rule as daily_run.bat: chcp 65001 + cmd batch pointer
rem   drift -> any non-ASCII byte silently mangles LATER lines.)
rem ---------------------------------------------------------------
rem  Start the agentskill web dashboard (http://127.0.0.1:8000/).
rem  Uses the 64-bit Python (needs fastapi / uvicorn / akshare).
rem  Local only. If you ever expose it beyond this machine, set
rem  AGENTSKILL_TOKEN first (server.py refuses public sharing
rem  without it - see the startup warning).
rem  PYTHON64 env var overrides the interpreter path.
rem ---------------------------------------------------------------
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1
if "%PYTHON64%"=="" set "PY64=E:\Python\python.exe"
if not "%PYTHON64%"=="" set "PY64=%PYTHON64%"
"%PY64%" server.py
