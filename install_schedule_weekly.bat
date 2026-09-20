@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo   Register WEEKLY research-data refresh
echo   Task name: QuantPanorama_WeeklyData
echo   Target   : weekly_data.bat  (dividends / industry / indexes)
echo   Schedule : every SAT 10:00
echo ============================================
echo.
echo WHY SATURDAY 10:00
echo   These three datasets do not change intraday and are NOT used by
echo   the 14:50 auto-trade path, so there is no reason to compete with it.
echo   Saturday is also the day the paper-tracking / factor review is done.
echo.
echo WHY NOT 14:50
echo   daily_run.bat already runs then (refresh ETF quotes + trade).
echo   Keeping the two apart means one failing cannot mask the other.
echo.
schtasks /create /f /tn "QuantPanorama_WeeklyData" /tr "\"%~dp0weekly_data.bat\"" /sc weekly /d SAT /st 10:00
if %errorlevel%==0 (
  echo [OK] Registered.
  echo      Verify with:  schtasks /query /tn "QuantPanorama_WeeklyData"
  echo      Run once now: schtasks /run   /tn "QuantPanorama_WeeklyData"
) else (
  echo [FAIL] Registration failed. Try right-click - "Run as administrator".
)
echo.
pause
