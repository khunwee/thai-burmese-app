"""
setup_env.py — ตัวช่วยตั้งค่าไฟล์ .env แบบถาม-ตอบ (รันด้วย 0_setup_env.bat)

ถามทีละข้อ · เปิดเว็บที่ต้องใช้ให้ · ทดสอบค่าที่ใส่ทันที (ฐานข้อมูล / Apps Script / Gemini)
ก่อนบันทึก จะสำรอง .env เดิมไว้เป็น .env.bak
กด Enter เฉย ๆ = ใช้ค่าเดิม (หรือข้ามข้อที่ไม่บังคับ)
"""
import datetime as dt
import getpass
import os
import re
import secrets
import sys
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
sys.path.insert(0, HERE)
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ENV = os.path.join(HERE, ".env")
KNOWN = ["DATABASE_URL", "JWT_SECRET", "MYMEMORY_EMAIL", "GAS_URL", "GAS_SECRET", "GEMINI_API_KEY"]


def read_env(path):
    vals, order = {}, []
    if os.path.exists(path):
        for line in open(path, encoding="utf-8-sig"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            vals[k] = v
            order.append(k)
    return vals, order


def mask(v):
    if not v:
        return "(ว่าง)"
    return v[:6] + "…" + v[-4:] if len(v) > 14 else "****"


def ask(prompt, current="", secret=False, url=None):
    """Enter = keep current. 'o' = open the website for this step."""
    while True:
        hint = f" [Enter = ใช้ค่าเดิม {mask(current)}]" if current else " [Enter = ข้าม]"
        if url:
            hint += " [o = เปิดเว็บ]"
        val = (getpass.getpass(f"{prompt}{hint}: ") if secret else input(f"{prompt}{hint}: ")).strip()
        if url and val.lower() == "o":
            webbrowser.open(url)
            print("   เปิดเว็บให้แล้ว — ทำตามขั้นตอนด้านบน แล้วกลับมาวางค่าที่นี่")
            continue
        return val or current


def title(t):
    print("\n" + "=" * 70 + f"\n {t}\n" + "=" * 70)


vals, order = read_env(ENV)
print("ตัวช่วยตั้งค่า .env — วุ้นแปลภาษาพม่า")
print("ค่าทุกข้อฟรี ไม่ต้องใช้บัตรเครดิต · วางข้อความด้วยคลิกขวา หรือ Ctrl+V")

import main  # noqa: E402  (for URL normalising + Thai error explanations)
import translators as tr  # noqa: E402
import logging  # noqa: E402
logging.getLogger().setLevel(logging.CRITICAL)

# ------------------------------------------------------------------ 1. DATABASE_URL
title("1/5 ฐานข้อมูล Supabase (DATABASE_URL) — ต้องมี")
print("""  1) supabase.com → New project · Region: Southeast Asia (Singapore) · จดรหัส Database Password
  2) ปุ่ม Connect (ด้านบน) → แท็บ Connection String → เลือก Session pooler
  3) คัดลอกข้อความที่ขึ้นต้นด้วย postgresql://postgres.  มาวางด้านล่าง
     (มีคำว่า [YOUR-PASSWORD] ได้ — โปรแกรมจะถามรหัสผ่านแยกให้)""")
cur_db = vals.get("DATABASE_URL", "")
if main.placeholder_db_url(cur_db):
    cur_db = ""
while True:
    db = ask("วาง Connection string", cur_db, url="https://supabase.com/dashboard/projects")
    if not db:
        print("   ! ข้ามฐานข้อมูล — โปรแกรมจะยังใช้งานไม่ได้จนกว่าจะใส่ค่านี้")
        break
    db = db.strip().strip('"').strip("'")
    if not re.match(r"^postgres(ql)?://", db):
        print("   ✗ ต้องขึ้นต้นด้วย postgresql://  — คัดลอกใหม่จาก Supabase")
        continue
    if "[YOUR-PASSWORD]" in db or "YOUR_PASSWORD" in db.upper():
        pw = getpass.getpass("   รหัสผ่านฐานข้อมูล (พิมพ์แล้วจะไม่แสดงบนจอ): ").strip()
        if not pw:
            continue
        db = db.replace("[YOUR-PASSWORD]", pw).replace("YOUR_PASSWORD", pw)
    if "xxxxxxxx" in db.lower():
        print("   ✗ ยังเป็นค่าตัวอย่าง (xxxxxxxx) — ต้องคัดลอกจากโปรเจกต์ของคุณ")
        continue
    host = re.sub(r"^.*@", "", db).split("/")[0]
    if ".supabase.co" in host and "pooler" not in host:
        print("   ! นี่คือ Direct connection (db.xxx.supabase.co) — Render ใช้ไม่ได้ ให้เลือก Session pooler แทน")
        if input("   ใช้ต่อเฉพาะในเครื่องนี้? (y/N): ").strip().lower() != "y":
            continue
    print("   กำลังทดสอบเชื่อมต่อ ...")
    try:
        import psycopg2
        conn = psycopg2.connect(main._normalize_db_url(db))
        with conn.cursor() as c:
            c.execute("SELECT current_database(), version()")
            dbname, ver = c.fetchone()
            c.execute("SELECT to_regclass('public.users') IS NOT NULL")
            has_users = c.fetchone()[0]
            n = None
            if has_users:
                c.execute("SELECT COUNT(*) FROM users")
                n = c.fetchone()[0]
        conn.close()
        print(f"   ✓ เชื่อมต่อได้: {dbname} · {ver.split(' on ')[0]}")
        print(f"     {'มีผู้ใช้อยู่แล้ว ' + str(n) + ' บัญชี (ฐานข้อมูลเดิม)' if has_users else 'ฐานข้อมูลใหม่ — จะได้ admin / 123456 ตอนเปิดโปรแกรมครั้งแรก'}")
        break
    except Exception as e:
        print("   ✗ " + main.explain_db_error(e))
        print("     รายละเอียด: " + str(e).strip().splitlines()[0][:160])
        if input("   ลองใหม่? (Y/n): ").strip().lower() == "n":
            break
        cur_db = ""
vals["DATABASE_URL"] = db

# ------------------------------------------------------------------ 2. JWT_SECRET
title("2/5 JWT_SECRET — สร้างให้อัตโนมัติ")
js = vals.get("JWT_SECRET", "")
if len(js) < 32 or "change-me" in js:
    js = secrets.token_urlsafe(48)
    print("   ✓ สร้างค่าสุ่มยาว 64 ตัวให้แล้ว (ใช้ค่าเดียวกันนี้ใน Render → Environment ได้)")
else:
    print("   ✓ มีค่าที่ใช้ได้อยู่แล้ว — ใช้ค่าเดิม")
vals["JWT_SECRET"] = js

# ------------------------------------------------------------------ 3. MyMemory
title("3/5 MYMEMORY_EMAIL — ไม่ต้องสมัคร แค่ใส่อีเมลจริง (5,000 → 50,000 ตัวอักษร/วัน)")
while True:
    em = ask("อีเมลของคุณ", vals.get("MYMEMORY_EMAIL", ""))
    if not em or re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", em):
        break
    print("   ✗ รูปแบบอีเมลไม่ถูกต้อง")
vals["MYMEMORY_EMAIL"] = em

# ------------------------------------------------------------------ 4. Apps Script
title("4/5 Google Apps Script (GAS_URL + GAS_SECRET) — แนะนำที่สุด แก้ปัญหา Google 429")
print("""  1) script.google.com → New project → ลบโค้ดเดิม → วางโค้ดจาก google_apps_script.gs
  2) แก้บรรทัด const SECRET = '...' เป็นรหัสลับของคุณ → Save
  3) Deploy → New deployment → Web app · Execute as: Me · Who has access: Anyone → Deploy → Authorize
     (ถ้าเจอ "Google hasn't verified this app" → Advanced → Go to … → Allow)
  4) คัดลอก Web app URL (ลงท้าย /exec)""")
while True:
    gu = ask("วาง Web app URL", vals.get("GAS_URL", ""), url="https://script.google.com/home/projects/create")
    if not gu:
        break
    if not re.match(r"^https://script\.google\.com/macros/s/[^/]+/exec$", gu):
        print("   ✗ ต้องเป็นรูปแบบ https://script.google.com/macros/s/…/exec (ใช้ URL ของ Web app ไม่ใช่ลิงก์หน้าแก้โค้ด)")
        continue
    gs = ask("รหัสลับ (ตรงกับ SECRET ในสคริปต์)", vals.get("GAS_SECRET", ""))
    os.environ["GAS_URL"], os.environ["GAS_SECRET"] = gu, gs
    print("   กำลังทดสอบแปล 'สวัสดีครับ' ผ่าน Apps Script ...")
    try:
        out = tr.PROVIDERS["gas"].translate("สวัสดีครับ")
        print("   ✓ ใช้ได้: " + out[:60])
        break
    except Exception as e:
        msg = str(e)
        head, _, fix = msg.partition(" — ")
        print("   ✗ " + head)
        if fix:
            print("     วิธีแก้: " + fix)
        if input("   ลองใหม่? (Y/n): ").strip().lower() == "n":
            break
if gu:
    vals["GAS_URL"], vals["GAS_SECRET"] = gu, gs

# ------------------------------------------------------------------ 5. Gemini
title("5/5 Gemini API key — ไม่บังคับ (ตัวสำรองคุณภาพดี)")
print("  aistudio.google.com/apikey → Create API key → คัดลอกคีย์ (ขึ้นต้น AIza…)")
while True:
    gk = ask("วาง Gemini API key", vals.get("GEMINI_API_KEY", ""), url="https://aistudio.google.com/apikey")
    if not gk:
        break
    os.environ["GEMINI_API_KEY"] = gk
    print("   กำลังทดสอบ ...")
    try:
        out = tr.PROVIDERS["gemini"].translate("สวัสดีครับ")
        print("   ✓ ใช้ได้: " + out[:60])
        break
    except Exception as e:
        print("   ✗ " + str(e)[:200])
        if "รุ่น" in str(e):
            print("     (คีย์อาจถูก แต่รุ่นที่ลองใช้ไม่ได้ — ใส่ GEMINI_MODEL=<ชื่อรุ่นจาก AI Studio> ใน .env ภายหลังได้)")
        if input("   ลองใหม่? (Y/n): ").strip().lower() == "n":
            break
vals["GEMINI_API_KEY"] = gk

# ------------------------------------------------------------------ write
if os.path.exists(ENV):
    try:
        import shutil
        shutil.copyfile(ENV, ENV + ".bak")
    except Exception:
        pass
lines = [f"# สร้างโดย 0_setup_env.bat เมื่อ {dt.datetime.now():%Y-%m-%d %H:%M} — ห้ามอัปโหลดไฟล์นี้ขึ้น GitHub",
         "# ค่าชุดเดียวกันนี้ต้องใส่ใน Render → Environment ตอน deploy จริง"]
for k in KNOWN:
    v = vals.get(k, "")
    lines.append(f"{k}={v}" if v else f"# {k}=")
others = [k for k in order if k not in KNOWN]
if others:
    lines.append("")
    lines.append("# ---- ค่าอื่นที่มีอยู่เดิม ----")
    lines += [f"{k}={vals[k]}" for k in others]
lines += ["", "# ค่าไม่บังคับ: เอา # ออกเพื่อใช้งาน",
          "# ADMIN_PASSWORD=รหัส-admin-ตอนฐานข้อมูลใหม่", "# GEMINI_MODEL=gemini-3.5-flash-lite",
          "# CF_ACCOUNT_ID=", "# CF_API_TOKEN=", "# TRANSLATE_ORDER=gas,google,google2,gemini,mymemory,cloudflare,googleweb"]
open(ENV, "w", encoding="utf-8", newline="\n").write("\n".join(lines) + "\n")

title("บันทึก .env แล้ว")
for k in KNOWN:
    print(f"  {k:<16} {'✓ ' + mask(vals.get(k, '')) if vals.get(k) else '— ไม่ได้ตั้ง'}")
print("\nขั้นต่อไป: 4_diagnose.bat (ตรวจทั้งระบบ) → 2_start.bat (เปิดโปรแกรม)")
