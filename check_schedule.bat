@echo off
rem ===============================================================
rem  Read-only health check of the daily auto-trade scheduled task.
rem  Changes NOTHING (no register / no delete / no run).
rem  Safe to double-click any time.
rem
rem  Checks: task exists on disk? key settings still right? action
rem  path still points at THIS repo? did it actually run and succeed?
rem
rem  Exit code: 0 = OK, 1 = needs attention (also printed).
rem  Real work: tools\schedule\check_task.ps1
rem ===============================================================
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PS1=tools\schedule\check_task.ps1"

echo.
if not exist "%PS1%" (
  echo [FAIL] missing %PS1%
  echo.
  pause
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%"
set "RC=%errorlevel%"

echo.
if "%RC%"=="0" (
  echo [i] exit code = 0   -- nothing to do.
) else (
  echo [i] exit code = %RC%   -- needs attention. Fix with install_schedule.bat
)
echo.
pause
exit /b %RC%
