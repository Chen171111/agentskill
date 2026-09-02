@echo off
cd /d "%~dp0"
echo ============================================
echo   Install daily auto-trade at 14:50 (THS)
echo ============================================
echo This task controls Tonghuashun (THS) simulated trading,
echo so it must run with admin and THS must be open at 14:50.
echo.
echo If it says access denied, right-click this file and
echo choose "Run as administrator", then retry.
echo.
schtasks /create /f /tn "QuantPanorama_DailyRun" /tr "%~dp0daily_run.bat" /sc weekly /d MON,TUE,WED,THU,FRI /st 14:50 /rl HIGHEST
if %errorlevel%==0 (echo [OK] Registered with highest privileges.) else (echo [FAIL] See above. Try run-as-administrator.)
echo.
pause