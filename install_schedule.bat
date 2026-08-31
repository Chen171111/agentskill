@echo off
cd /d "%~dp0"
echo ============================================
echo   Install daily auto-run at 14:50 (Mon-Fri)
echo ============================================
echo If it says access denied, right-click this file
echo and choose "Run as administrator", then retry.
echo.
schtasks /create /f /tn "QuantPanorama_DailyRun" /tr "%~dp0daily_run.bat" /sc weekly /d MON,TUE,WED,THU,FRI /st 14:50
if %errorlevel%==0 (echo [OK] Registered.) else (echo [FAIL] See above. Try run-as-administrator.)
echo.
pause