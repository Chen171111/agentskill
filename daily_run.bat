@echo off
cd /d "%~dp0"
echo ===== %date% %time% ===== >> state\daily_run.log
"E:\Python\python.exe" main.py simulate >> state\daily_run.log 2>&1