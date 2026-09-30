@echo off
rem ===============================================================
rem  Register / repair the daily auto-trade scheduled task.
rem  Task     : QuantPanorama_DailyRun
rem  Action   : daily_run.bat   (Mon-Fri 14:50 local time)
rem  Real work is done by tools\schedule\register_task.ps1, which
rem  also verifies the ON-DISK definition (not just "the name is
rem  queryable" - that yardstick gave a false OK on 2026-09-23).
rem
rem  History: the old version of this file used
rem    schtasks /create /sc weekly ...
rem  On 2026-09-24 that form was measured DENIED for a normal user
rem  on this machine, and on 2026-09-23 a same-name task with the OLD
rem  definition made "query succeeds" look like success. Replaced.
rem
rem  NOTE: THS client must be OPEN and LOGGED IN at 14:50.
rem  Nothing is traded by this script - registration only.
rem ===============================================================
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "LOG=state\install_schedule.log"
if not exist state mkdir state
set "PS1=tools\schedule\register_task.ps1"
set "ELEVATED=0"
net session >nul 2>&1
if not errorlevel 1 set "ELEVATED=1"

echo ==============================================================
echo   Register / repair daily auto-trade task
echo     task      : QuantPanorama_DailyRun
echo     action    : %~dp0daily_run.bat
echo     schedule  : Mon-Fri 14:50 local
echo     elevated  : %ELEVATED%   (0 = normal user; that is FINE)
echo ==============================================================
echo.

if not exist "%PS1%" (
  echo [FAIL] missing %PS1%
  echo [FAIL] missing %PS1% >> "%LOG%"
  goto :end
)

echo ============================================================ >> "%LOG%"
echo %date% %time%  install_schedule run (elevated=%ELEVATED%) >> "%LOG%"

powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%"
set "RC=%errorlevel%"
echo.
echo [i] register_task.ps1 exit code = %RC%
echo [i] register_task.ps1 exit code = %RC% >> "%LOG%"
if "%RC%"=="0" goto :ok
if "%ELEVATED%"=="1" goto :giveup

echo.
echo --------------------------------------------------------------
echo   Registration was denied for a normal user on this machine.
echo   Relaunching ELEVATED - a UAC prompt will appear, click YES.
echo   The registration is re-attempted in the new window.
echo --------------------------------------------------------------
echo.
powershell -NoProfile -Command "try{Start-Process -FilePath '%~f0' -ArgumentList 'elevated' -Verb RunAs -WorkingDirectory '%~dp0'; exit 0}catch{Write-Output $_.Exception.Message; exit 1}"
if errorlevel 1 goto :uacdeclined
echo [i] Continue in the NEW elevated window; this one can be closed.
echo [i] relaunched elevated >> "%LOG%"
goto :end

:uacdeclined
echo [FAIL] UAC was declined or elevation failed.
echo [FAIL] UAC declined or elevation failed >> "%LOG%"
goto :giveup

:ok
echo.
echo ==============================================================
echo   [OK] registered AND the on-disk definition was verified.
echo ==============================================================
echo   * Verify any time (read-only) : check_schedule.bat
echo   * Run the task once right now : schtasks /run /tn "QuantPanorama_DailyRun"
echo   * Remove the task            : schtasks /delete /f /tn "QuantPanorama_DailyRun"
echo   * THS client must be OPEN and LOGGED IN at 14:50.
echo.
echo   [IMPORTANT] Registration is proven only for THIS moment.
echo   A task on this machine once vanished silently. Re-check with
echo   check_schedule.bat after the next weekday.
echo.
echo [OK] registered and on-disk definition verified >> "%LOG%"
goto :end

:giveup
echo.
echo --------------------------------------------------------------
echo   Could not register from here. Two manual routes:
echo     A. Right-click install_schedule.bat - "Run as administrator"
echo     B. Task Scheduler GUI - Action - ImportTask -
echo        %~dp0tools\schedule\QuantPanorama_DailyRun.xml
echo        (then run check_schedule.bat to verify)
echo --------------------------------------------------------------
echo.
echo [FAIL] manual intervention needed >> "%LOG%"

:end
echo.
echo This window stays open on purpose - read the lines above.
pause
exit /b 0
