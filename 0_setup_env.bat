@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Thai-Burmese : SETUP .env
if not exist ".venv\Scripts\python.exe" (
  echo [ผิดพลาด] ยังไม่ได้ติดตั้ง - รัน 1_install.bat ก่อน
  pause & exit /b 1
)
if not exist setup_env.py (
  echo [ผิดพลาด] ไม่พบ setup_env.py ในโฟลเดอร์นี้
  pause & exit /b 1
)
set PYTHONIOENCODING=utf-8
".venv\Scripts\python.exe" setup_env.py
echo.
pause
