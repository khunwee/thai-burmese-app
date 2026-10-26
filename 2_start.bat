@echo off
chcp 65001 >nul
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title Thai-Burmese : START
rem ===== ตั้งค่า =====
set "PORT=8000"
set "RELOAD=1"
rem RELOAD=1 = แก้ไฟล์ .py/.html แล้วเซิร์ฟเวอร์รีสตาร์ตเอง (เหมาะกับการทดสอบ)

if not exist ".venv\Scripts\python.exe" (
  echo [ผิดพลาด] ยังไม่ได้ติดตั้ง - รัน 1_install.bat ก่อน
  pause & exit /b 1
)
if not exist .env (
  echo [ผิดพลาด] ไม่มีไฟล์ .env - รัน 1_install.bat ก่อน
  pause & exit /b 1
)
findstr /i /c:"YOUR_PASSWORD" /c:"xxxxxxxx" .env >nul 2>&1
if not errorlevel 1 (
  echo [เตือน] DATABASE_URL ใน .env ยังเป็นค่าตัวอย่าง - หน้าเว็บจะล็อกอินไม่ได้
  echo   แก้ง่ายที่สุด: รัน 0_setup_env.bat
  choice /c YN /m "เปิด 0_setup_env.bat ตอนนี้เลยไหม"
  if not errorlevel 2 (
    call "%~dp00_setup_env.bat"
  )
)
if not exist logs mkdir logs
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

rem ---------- พอร์ตว่างไหม ----------
set "BUSY="
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /r /c:":%PORT% .*LISTENING"') do set "BUSY=%%p"
if defined BUSY (
  curl.exe -s -m 3 http://127.0.0.1:%PORT%/healthz | findstr /c:"\"ok\":true" >nul 2>&1
  if not errorlevel 1 (
    echo โปรแกรมเปิดอยู่แล้ว - เปิดเบราว์เซอร์ให้
    start "" http://127.0.0.1:%PORT%/
    exit /b 0
  )
  echo [ผิดพลาด] พอร์ต %PORT% ถูกโปรแกรมอื่นใช้อยู่ (PID !BUSY!^)
  echo   แก้ไข: รัน 3_stop.bat หรือเปลี่ยน set "PORT=8000" ในไฟล์นี้เป็นเลขอื่น
  pause & exit /b 1
)

rem ---------- เก็บ log รอบก่อน ----------
if exist logs\server.log move /y logs\server.log logs\server_prev.log >nul

set "ARGS=main:app --host 127.0.0.1 --port %PORT%"
if "%RELOAD%"=="1" set "ARGS=!ARGS! --reload --reload-include *.html"
echo กำลังเปิดเซิร์ฟเวอร์ที่ http://127.0.0.1:%PORT% ...
start "TB_SERVER" /min cmd /c ""%~dp0.venv\Scripts\python.exe" -m uvicorn !ARGS! > "%~dp0logs\server.log" 2>&1"

rem ---------- รอจน /healthz ตอบ (สูงสุด 60 วินาที) ----------
set /a WAIT=0
:waitloop
timeout /t 2 /nobreak >nul
set /a WAIT+=2
curl.exe -s -m 3 -o logs\healthz.json -w "%%{http_code}" http://127.0.0.1:%PORT%/healthz > logs\healthz.code 2>nul
set /p CODE=<logs\healthz.code
if "!CODE!"=="200" goto :up
if !WAIT! GEQ 60 goto :timeout
<nul set /p "=."
goto :waitloop

:up
echo.
echo เซิร์ฟเวอร์พร้อมแล้ว (!WAIT! วินาที)
type logs\healthz.json
echo.
findstr /c:"\"db_ok\":true" logs\healthz.json >nul || echo   * เตือน: ฐานข้อมูลยังเชื่อมต่อไม่ได้ - ดู logs\server.log หรือรัน 4_diagnose.bat
start "" http://127.0.0.1:%PORT%/
echo.
echo   หน้าเว็บ : http://127.0.0.1:%PORT%/
echo   Log      : logs\server.log  (หน้าต่าง TB_SERVER ถูกย่อไว้ที่ Taskbar)
echo   ปิดโปรแกรม: 3_stop.bat
echo   ห้ามเปิด index.html ด้วยการดับเบิลคลิก - ต้องเปิดผ่านลิงก์ด้านบนเท่านั้น
timeout /t 8 >nul
exit /b 0

:timeout
echo.
echo [ผิดพลาด] เซิร์ฟเวอร์ไม่ตอบภายใน 60 วินาที - 30 บรรทัดสุดท้ายของ logs\server.log:
echo ------------------------------------------------------------
powershell -NoProfile -Command "Get-Content -Path 'logs\server.log' -Tail 30 -Encoding UTF8"
echo ------------------------------------------------------------
echo   ดูสาเหตุด้านบน หรือรัน 4_diagnose.bat
pause
exit /b 1
