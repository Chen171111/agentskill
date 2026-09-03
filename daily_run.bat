@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
echo ===== %date% %time% ===== >> state\daily_run.log
"E:\Python\python.exe" main.py simulate --ths >> state\daily_run.log 2>&1