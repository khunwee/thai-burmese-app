@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Thai-Burmese : DIAGNOSE
if not exist ".venv\Scripts\python.exe" (
  echo [ผิดพลาด] ยังไม่ได้ติดตั้ง - รัน 1_install.bat ก่อน
  pause & exit /b 1
)
if not exist local_diagnose.py (
  echo [ผิดพลาด] ไม่พบ local_diagnose.py ในโฟลเดอร์นี้
  pause & exit /b 1
)
set PYTHONIOENCODING=utf-8
echo กำลังตรวจทุกส่วน (ใช้เวลา ~1 นาที, ต้องต่ออินเทอร์เน็ต) ...
".venv\Scripts\python.exe" local_diagnose.py
echo.
if exist logs\samples start "" "%~dp0logs\samples"
pause
