@echo off
chcp 65001 >nul
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title Thai-Burmese : INSTALL
if not exist logs mkdir logs
set "LOG=logs\install.log"
echo ==== INSTALL %date% %time% ==== > "%LOG%"

echo.
echo ============================================================
echo   วุ้นแปลภาษาพม่า - ติดตั้งสำหรับทดสอบในเครื่อง
echo ============================================================
echo.

rem ---------- 1) ตรวจไฟล์โปรแกรม (บอกทีละไฟล์) ----------
set "MISSING="
for %%F in (main.py translators.py index.html) do (
  if not exist "%%F" (
    echo [ผิดพลาด] ไม่พบไฟล์ %%F
    set "MISSING=1"
  )
)
if defined MISSING goto :nofiles
if not exist requirements.txt (
  echo [แก้ให้] ไม่พบ requirements.txt - สร้างให้อัตโนมัติ
  > requirements.txt echo fastapi^>=0.110,^<1.0
  >> requirements.txt echo uvicorn[standard]^>=0.29
  >> requirements.txt echo python-multipart^>=0.0.9
  >> requirements.txt echo psycopg2-binary^>=2.9.9
  >> requirements.txt echo PyJWT^>=2.8
  >> requirements.txt echo requests^>=2.31
  >> requirements.txt echo deep-translator^>=1.11.4
  >> requirements.txt echo imageio-ffmpeg^>=0.5.1
  >> requirements.txt echo edge-tts^>=7.0.0
)
if not exist .python-version (
  > .python-version echo 3.11
  echo [แก้ให้] สร้าง .python-version (3.11^) ให้แล้ว - ต้องอัปโหลดไฟล์นี้ขึ้น GitHub ด้วย
)

rem ---------- 2) หา Python (แนะนำ 3.11 ให้ตรงกับ Render) ----------
set "PY="
for %%V in (3.11 3.12 3.13) do (
  if not defined PY (
    py -%%V -c "import sys" >nul 2>&1 && set "PY=py -%%V"
  )
)
if not defined PY (
  python -c "import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1 && set "PY=python"
)
if not defined PY goto :nopython
for /f "delims=" %%i in ('!PY! -c "import sys;print(sys.version.split()[0])"') do set "PYVER=%%i"
echo [1/4] ใช้ Python !PYVER!  (คำสั่ง: !PY!)
echo Python !PYVER! via !PY! >> "%LOG%"

rem ---------- 3) สร้าง virtual environment (.venv) ----------
if exist ".venv\Scripts\python.exe" (
  echo [2/4] มี .venv อยู่แล้ว - ใช้ของเดิม
) else (
  echo [2/4] กำลังสร้าง .venv ...
  !PY! -m venv .venv >> "%LOG%" 2>&1
  if errorlevel 1 goto :venvfail
)

rem ---------- 4) ติดตั้งไลบรารี ----------
echo [3/4] กำลังติดตั้งไลบรารี (ครั้งแรกใช้เวลา 1-3 นาที) ...
".venv\Scripts\python.exe" -m pip install --upgrade pip >> "%LOG%" 2>&1
".venv\Scripts\python.exe" -m pip install -r requirements.txt >> "%LOG%" 2>&1
if errorlevel 1 goto :pipfail
".venv\Scripts\python.exe" -m pip install -U edge-tts >> "%LOG%" 2>&1
echo       ติดตั้งเสร็จ

rem ---------- 5) ไฟล์ .env ----------
if exist .env (
  echo [4/4] มีไฟล์ .env อยู่แล้ว
) else (
  if exist .env.example (
    copy /y .env.example .env >nul
    echo [4/4] สร้างไฟล์ .env จาก .env.example แล้ว
    echo       กรุณาแก้ DATABASE_URL ให้เป็น Supabase connection string จริง แล้วกด Save
    start "" notepad .env
  ) else (
    echo [4/4] ไม่พบ .env.example - สร้าง .env เปล่าให้
    echo DATABASE_URL=> .env
    echo JWT_SECRET=change-me-to-a-long-random-string-at-least-32-chars>> .env
    start "" notepad .env
  )
)
findstr /c:"YOUR_PASSWORD" .env >nul 2>&1 && echo   * เตือน: DATABASE_URL ใน .env ยังเป็นค่าตัวอย่าง (YOUR_PASSWORD)

echo.
echo ============================================================
echo   ติดตั้งเสร็จ  ขั้นต่อไป:
echo     0_setup_env.bat = ตั้งค่า .env แบบถาม-ตอบ (ทดสอบให้ทันที)
echo     4_diagnose.bat  = ตรวจทุกส่วน + บันทึกตัวอย่างเสียง
echo     2_start.bat     = เปิดโปรแกรม (เปิดเบราว์เซอร์ให้อัตโนมัติ)
echo     3_stop.bat      = ปิดโปรแกรม
echo   บันทึกการติดตั้ง: %LOG%
echo ============================================================
pause
exit /b 0

:nofiles
echo   ให้วางไฟล์ .bat ไว้ในโฟลเดอร์เดียวกับ main.py และคัดลอกไฟล์ที่ขาดมาจากโฟลเดอร์ V15
pause & exit /b 1
:nopython
echo [ผิดพลาด] ไม่พบ Python 3.10 ขึ้นไป
echo   ติดตั้ง Python 3.11 จาก https://www.python.org/downloads/
echo   ตอนติดตั้งให้ติ๊ก "Add python.exe to PATH"
start "" https://www.python.org/downloads/release/python-3119/
pause & exit /b 1
:venvfail
echo [ผิดพลาด] สร้าง .venv ไม่สำเร็จ - ดูรายละเอียดใน %LOG%
pause & exit /b 1
:pipfail
echo [ผิดพลาด] ติดตั้งไลบรารีไม่สำเร็จ - 20 บรรทัดสุดท้ายของ %LOG%:
powershell -NoProfile -Command "Get-Content -Path '%LOG%' -Tail 20"
echo   สาเหตุที่พบบ่อย: ไม่มีอินเทอร์เน็ต / Proxy บริษัท / Python 3.14 ใหม่เกินไป
pause & exit /b 1
