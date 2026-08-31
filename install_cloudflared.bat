@echo off
cd /d "%~dp0"
echo ============================================
echo   Install cloudflared (one-time)
echo ============================================
echo Requires access to GitHub. Turn on proxy if needed.
echo.
if not exist tools mkdir tools
echo Downloading cloudflared ...
curl.exe -L -o tools\cloudflared.exe https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe
echo.
if exist tools\cloudflared.exe (
  echo [OK] installed: tools\cloudflared.exe
) else (
  echo [FAIL] download failed. Enable proxy and retry, or use SSH tunnel in share.bat.
)
echo.
pause