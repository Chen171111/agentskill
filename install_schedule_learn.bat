@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo   Register DAILY learning loop (M4)
echo   Task name: QuantPanorama_DailyLearn
echo   Target   : daily_learn.bat  (data delta / features / labels / monitor)
echo   Schedule : every day 18:10
echo ============================================
echo.
echo WHY 18:10 (not 14:50)
echo   Step 1 needs the DAY'S FINAL quotes, which only settle after the
echo   15:00 close. Running it before the close would ingest a partial bar
echo   and poison the label backfill. 18:10 leaves margin for the data
echo   provider to publish.
echo   It is also deliberately 3.5h AFTER daily_run.bat (14:50) so the two
echo   never contend -- and so a failure of one cannot be mistaken for the other.
echo.
echo RED LINE
echo   This loop only RECORDS. It must never change a finalized rule,
echo   never write state/trading.db, never place an order.
echo   "Evaluation may be automated; taking effect must be human-confirmed."
echo.
schtasks /create /f /tn "QuantPanorama_DailyLearn" /tr "\"%~dp0daily_learn.bat\"" /sc daily /st 18:10
if %errorlevel%==0 (
  echo [OK] Registered.
  echo      Verify with:  schtasks /query /tn "QuantPanorama_DailyLearn"
  echo      Run once now: schtasks /run   /tn "QuantPanorama_DailyLearn"
) else (
  echo [FAIL] Registration failed. Try right-click - "Run as administrator".
)
echo.
pause
