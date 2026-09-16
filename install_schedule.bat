@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo   Register daily auto-trade task at 14:50 (Mon-Fri)
echo   Task name: QuantPanorama_DailyRun
echo   Target   : daily_run.bat  (refresh data -^> trade)
echo ============================================
echo.
echo NOTE 1: Normal user level is enough. The THS automation is
echo         message-based (BM_CLICK / WM_CHAR), which does NOT
echo         need the window to be focused, so elevation is not
echo         required. If it ever reports access denied, right-click
echo         this file and choose "Run as administrator".
echo NOTE 2: THS client must be OPEN and LOGGED IN at 14:50.
echo         Closing it (or the PC sleeping) makes the run fail.
echo.
schtasks /create /f /tn "QuantPanorama_DailyRun" /tr "\"%~dp0daily_run.bat\"" /sc weekly /d MON,TUE,WED,THU,FRI /st 14:50
if %errorlevel%==0 (
  echo [OK] Registered.
  schtasks /query /tn "QuantPanorama_DailyRun"
) else (
  echo [FAIL] See message above. Try running this file as administrator.
)
echo.
echo To remove the task:  schtasks /delete /tn "QuantPanorama_DailyRun" /f
echo.
pause
