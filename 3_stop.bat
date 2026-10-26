@echo off
chcp 65001 >nul
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
set "PORT=8000"
set "FOUND="

rem ปิดหน้าต่างเซิร์ฟเวอร์ที่ 2_start.bat เปิดไว้ (รวม process ลูกของ --reload)
taskkill /FI "WINDOWTITLE eq TB_SERVER*" /T /F >nul 2>&1 && set "FOUND=1"

rem กันพลาด: ปิด process ที่ยังจับพอร์ตอยู่
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /r /c:":%PORT% .*LISTENING"') do (
  if not "%%p"=="0" (
    taskkill /PID %%p /T /F >nul 2>&1 && set "FOUND=1"
  )
)

timeout /t 1 /nobreak >nul
set "STILL="
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /r /c:":%PORT% .*LISTENING"') do set "STILL=%%p"
if defined STILL (
  echo [ผิดพลาด] ยังมีโปรแกรมใช้พอร์ต %PORT% อยู่ (PID !STILL!^) - ลองคลิกขวาไฟล์นี้แล้ว Run as administrator
  pause & exit /b 1
)
if defined FOUND (echo ปิดโปรแกรมเรียบร้อย) else (echo ไม่มีโปรแกรมเปิดอยู่)
timeout /t 3 >nul
exit /b 0
