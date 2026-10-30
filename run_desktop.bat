@echo off
setlocal
cd /d "%~dp0"
set "PY=E:\Python\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" -u quant_desktop.py %*
if errorlevel 1 pause
