@echo off
cd /d "%~dp0"
echo ============================================
echo   Share public link (temporary)
echo ============================================
rem 2026-10-02 acceptance fix (P1-5): refuse to start without a token.
rem The tunnel exposes the WHOLE panel to the public internet, including
rem /api/status (real positions) and /api/backtest (can be triggered by
rem anyone). Without AGENTSKILL_TOKEN the API is completely unauthenticated.
if "%AGENTSKILL_TOKEN%"=="" (
  echo.
  echo [REFUSED] AGENTSKILL_TOKEN is not set.
  echo           The public tunnel would expose real positions and the
  echo           backtest API to the internet with NO authentication.
  echo.
  echo           Set it first:  set AGENTSKILL_TOKEN=a-long-random-string
  echo           ^(and make sure server.py is restarted with it set^)
  echo.
  pause
  exit /b 1
)
echo Token check OK. Make sure start.bat is running first ^(with the SAME token^).
echo.
if exist "tools\cloudflared.exe" (
  echo [Mode 1] cloudflared tunnel ...
  echo A https://xxxx.trycloudflare.com link will appear below.
  echo Press Ctrl+C to stop.
  echo.
  tools\cloudflared.exe tunnel --url http://127.0.0.1:8000
) else (
  echo [Mode 2] SSH tunnel (no download needed) ...
  echo A https://xxxx.lhr.life link will appear below.
  echo Press Ctrl+C to stop.
  echo.
  ssh -o StrictHostKeyChecking=accept-new -R 80:127.0.0.1:8000 nokey@localhost.run
)
echo.
pause