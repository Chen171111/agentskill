@echo off
title QuantPanorama
cd /d "%~dp0"
echo ============================================
echo   QuantPanorama  (http://127.0.0.1:8000)
echo ============================================
echo Starting server, browser will open automatically.
echo Keep this window open; close it to stop.
python server.py
echo.
pause