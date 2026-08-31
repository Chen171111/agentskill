@echo off
cd /d "%~dp0"
echo ============================================
echo   Share public link (temporary)
echo ============================================
echo Make sure start.bat is running first.
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