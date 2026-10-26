"""
local_diagnose.py — ตรวจสุขภาพระบบ วุ้นแปลภาษาพม่า ก่อนอัปโหลดขึ้น host
รันด้วย 4_diagnose.bat (หรือ: python local_diagnose.py)

ตรวจ: Python · ไลบรารี · ไฟล์ .env · ฐานข้อมูล · ffmpeg · ช่องทางแปลทุกตัว · เสียงทุกแบบ
ผลลัพธ์: แสดงบนจอ + บันทึกที่ logs/diagnose_report.txt + ตัวอย่างเสียงที่ logs/samples/
ไม่ต้องเปิดเซิร์ฟเวอร์ก่อน (ใช้โค้ดใน main.py / translators.py ตรง ๆ)
"""
import asyncio
import importlib
import os
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
sys.path.insert(0, HERE)
LOG_DIR = os.path.join(HERE, "logs")
SAMPLE_DIR = os.path.join(LOG_DIR, "samples")
os.makedirs(SAMPLE_DIR, exist_ok=True)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

lines = []
results = {"ok": 0, "warn": 0, "fail": 0}


def out(msg=""):
    print(msg, flush=True)
    lines.append(msg)


def mark(kind, msg, hint=None):
    sym = {"ok": "[ OK ]", "warn": "[WARN]", "fail": "[FAIL]"}[kind]
    results[kind] += 1
    out(f"  {sym} {msg}")
    if hint:
        out(f"         -> {hint}")


def section(title):
    out("")
    out("=" * 70)
    out(" " + title)
    out("=" * 70)


# ---------------------------------------------------------------- 1. Python
section("1) Python")
v = sys.version_info
out(f"  Python {v.major}.{v.minor}.{v.micro}  ({sys.executable})")
if (v.major, v.minor) < (3, 10):
    mark("fail", "Python เก่าเกินไป", "ติดตั้ง Python 3.11 จาก python.org")
elif (v.major, v.minor) >= (3, 14):
    mark("warn", "Python 3.14 ใหม่มาก ไลบรารีบางตัวอาจยังไม่รองรับ", "แนะนำ 3.11 หรือ 3.12 (ตรงกับ .python-version บน Render)")
else:
    mark("ok", "เวอร์ชันใช้ได้")

# ---------------------------------------------------------------- 2. Packages
section("2) ไลบรารี (requirements.txt)")
missing = False
for mod, pip_name in [("fastapi", "fastapi"), ("uvicorn", "uvicorn"), ("multipart", "python-multipart"),
                      ("psycopg2", "psycopg2-binary"), ("jwt", "PyJWT"), ("requests", "requests"),
                      ("deep_translator", "deep-translator"), ("imageio_ffmpeg", "imageio-ffmpeg"),
                      ("edge_tts", "edge-tts")]:
    try:
        m = importlib.import_module(mod)
        ver = getattr(m, "__version__", "")
        mark("ok", f"{pip_name} {ver}".strip())
    except Exception as e:
        missing = True
        mark("fail", f"{pip_name} ไม่ได้ติดตั้ง ({e.__class__.__name__})", "รัน 1_install.bat ใหม่")

for f in ("main.py", "translators.py", "index.html", "requirements.txt"):
    if not os.path.exists(os.path.join(HERE, f)):
        mark("fail", f"ไม่พบไฟล์ {f} ในโฟลเดอร์นี้", "วาง .bat และ local_diagnose.py ไว้โฟลเดอร์เดียวกับ main.py")
        missing = True
if missing:
    out("\nหยุดตรวจ: ต้องแก้ข้อ [FAIL] ด้านบนก่อน")
    open(os.path.join(LOG_DIR, "diagnose_report.txt"), "w", encoding="utf-8").write("\n".join(lines))
    sys.exit(1)

# ---------------------------------------------------------------- 3. .env
section("3) ไฟล์ .env")
if not os.path.exists(os.path.join(HERE, ".env")):
    mark("fail", "ไม่มีไฟล์ .env", "รัน 1_install.bat (จะสร้างจาก .env.example ให้) แล้วแก้ DATABASE_URL")
import main  # noqa: E402  (loads .env)
import translators as tr  # noqa: E402
import logging  # noqa: E402
logging.getLogger().setLevel(logging.ERROR)  # results are reported below; keep the screen clean

db_url = os.getenv("DATABASE_URL", "")
if not db_url:
    mark("fail", "ยังไม่ได้ใส่ DATABASE_URL (แก้ง่ายสุด: รัน 0_setup_env.bat)",
         "เปิด .env ด้วย Notepad → ใส่ Supabase: Connect → Session pooler (ใช้ DB เดิมของเว็บจริง หรือสร้างโปรเจกต์ทดสอบแยก ฟรี 2 โปรเจกต์)")
elif main.placeholder_db_url(db_url):
    mark("fail", main.placeholder_db_url(db_url), "รัน 0_setup_env.bat — ถามทีละข้อและทดสอบการเชื่อมต่อให้ทันที")
else:
    info = main.describe_db()
    mark("ok", f"DATABASE_URL: {info['kind']} · host {info['host']}:{info['port']}")
    if info["kind"] == "supabase" and "pooler" not in (info["host"] or ""):
        mark("warn", "ใช้ Direct connection (db.xxx.supabase.co)", "บน Render ต้องใช้ Session pooler (…pooler.supabase.com)")
js = os.getenv("JWT_SECRET", "")
if len(js) < 32 or "change-me" in js:
    mark("warn", "JWT_SECRET สั้นหรือเป็นค่าตัวอย่าง", "ในเครื่องใช้ได้ (ระบบใช้ secret ในฐานข้อมูลแทน) แต่บน Render ควรตั้งยาว ≥ 32 ตัว")
else:
    mark("ok", "JWT_SECRET ตั้งแล้ว")
for k, why in [("MYMEMORY_EMAIL", "เพิ่มโควตา MyMemory เป็น 50,000 ตัว/วัน"), ("GAS_URL", "ตัวแปลส่วนตัว Apps Script (แนะนำ)"),
               ("GEMINI_API_KEY", "ช่องทาง Gemini (ไม่บังคับ)")]:
    if os.getenv(k):
        mark("ok", f"{k} ตั้งแล้ว")
    else:
        mark("warn", f"ยังไม่ได้ตั้ง {k}", why)

# ---------------------------------------------------------------- 4. Database
section("4) ฐานข้อมูล")
if db_url and main.placeholder_db_url(db_url):
    mark("fail", "ข้าม (DATABASE_URL ยังเป็นค่าตัวอย่าง)", "รัน 0_setup_env.bat")
elif db_url:
    t0 = time.time()
    try:
        ok = main.init_db(attempts=1)
    except Exception as e:
        ok = False
        main._db_last_error = str(e)
    if ok:
        def _q(c):
            c.execute("SELECT COUNT(*) AS n FROM users")
            n = c.fetchone()["n"]
            c.execute("SELECT username, role, is_active, must_change_password FROM users WHERE role='admin' ORDER BY id")
            return n, c.fetchall()
        n, admins = main.with_db(_q)
        mark("ok", f"เชื่อมต่อได้ ({int((time.time() - t0) * 1000)} ms) · ผู้ใช้ {n} บัญชี")
        for a in admins:
            state = "ใช้งาน" if a["is_active"] else "ปิดใช้งาน"
            extra = " · ยังต้องเปลี่ยนรหัส (อาจยังเป็น 123456)" if a["must_change_password"] else ""
            out(f"         admin: {a['username']} ({state}{extra})")
        try:
            ms = []
            for _ in range(3):
                t1 = time.time()
                main.with_db(lambda c: c.execute("SELECT 1"))
                ms.append((time.time() - t1) * 1000)
            avg = sum(ms) / len(ms)
            out(f"         การประกาศ 1 ครั้งคุยกับฐานข้อมูล ~4 รอบ ≈ {avg * 4:.0f} ms (บน Render Singapore เหลือ ~10–20 ms)")
            (mark("ok", f"ความเร็วต่อคำสั่ง ~{avg:.0f} ms") if avg < 150 else
             mark("warn", f"ความเร็วต่อคำสั่ง ~{avg:.0f} ms (ช้า)", "ปกติถ้าเน็ตบ้าน/ออฟฟิศไกลจาก Supabase; บน Render Singapore จะเร็วกว่า"))
        except Exception:
            pass
    else:
        mark("fail", "เชื่อมต่อฐานข้อมูลไม่ได้", str(main._db_last_error)[:220] + " · แก้ง่ายสุด: รัน 0_setup_env.bat")
else:
    mark("fail", "ข้าม (ไม่มี DATABASE_URL)")

# ---------------------------------------------------------------- 5. ffmpeg
section("5) ffmpeg (แปลงไฟล์เสียง / ปรับความเร็วเสียง Google)")
try:
    import imageio_ffmpeg
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    mark("ok", "พบ ffmpeg: " + exe)
except Exception as e:
    mark("fail", "ไม่พบ ffmpeg: " + str(e)[:150], "pip install --force-reinstall imageio-ffmpeg")

# ---------------------------------------------------------------- 6. Translation
section("6) ช่องทางแปลภาษา (ทดสอบจริงทุกช่องทาง)")
sample_th = "กรุณาสวมหมวกนิรภัยและรองเท้าเซฟตี้ก่อนเข้าพื้นที่ผลิต"
out(f"  ข้อความทดสอบ: {sample_th}")
working = 0
failed = []
for r in tr.test_all(sample_th):
    if r["ok"]:
        working += 1
        mark("ok", f"{r['label']} ({r.get('ms', 0)} ms): {r['result'][:70]}")
    elif "ยังไม่ได้ตั้งค่า" in r["result"]:
        out(f"  [ -- ] {r['label']}: {r['result']}")
    else:
        failed.append(r)
for r in failed:  # judged after we know whether a backup works
    limited = "429" in r["result"] or "จำกัด" in r["result"] or "โควตา" in r["result"]
    if working:
        if limited:
            mark("warn", f"{r['label']}: {r['result'][:110]}",
                 "ผู้ให้บริการจำกัด IP นี้ชั่วคราว (ปกติกับเน็ตออฟฟิศที่ใช้ IP ร่วมกัน) — ระบบพักช่องทางนี้และใช้ตัวสำรองให้อัตโนมัติ")
        else:  # a setting problem: show the whole fix, and say the system skips it meanwhile
            head, _, fix = r["result"].partition(" — ")
            mark("warn", f"{r['label']}: {head[:110]}", (fix or "ตรวจการตั้งค่า") + "  (ระหว่างนี้ระบบใช้ช่องทางอื่นแทนอัตโนมัติ)")
    else:
        mark("fail", f"{r['label']}: {r['result'][:150]}")
if working == 0:
    mark("fail", "ไม่มีช่องทางแปลที่ใช้ได้เลย", "เช็กอินเทอร์เน็ต / Proxy / Firewall ของบริษัท")
elif working < 2:
    mark("warn", f"ใช้ได้แค่ {working} ช่องทาง", "ตั้ง GAS_URL หรือ MYMEMORY_EMAIL เพิ่มเพื่อมีตัวสำรอง")
else:
    mark("ok", f"มีช่องทางแปลใช้ได้ {working} ช่องทาง")

# ---------------------------------------------------------------- 7. Voices
section("7) เสียงพูด (บันทึกตัวอย่างไว้ฟังเทียบที่ logs\\samples)")
sample_my = "ကျေးဇူးပြု၍ ထုတ်လုပ်ရေးဧရိယာထဲသို့ မဝင်မီ လုံခြုံရေးဦးထုပ်နှင့် ဘေးကင်းဖိနပ်ကို ဝတ်ဆင်ပါ။"
# (voice, rate, pace, text, file name) — pairs show what "tight" pace changes
tests = [("my-MM-NilarNeural", "+15%", "original", sample_my, "1_nilar_+15_original"),
         ("my-MM-NilarNeural", "+15%", "tight", sample_my, "2_nilar_+15_tight"),
         ("my-MM-ThihaNeural", "+15%", "tight", sample_my, "3_thiha_+15_tight"),
         ("google-my", "+15%", "original", sample_my, "4_google_+15_original"),
         ("google-my", "+15%", "tight", sample_my, "5_google_+15_tight"),
         ("google-my", "+25%", "tight", sample_my, "6_google_+25_tight"),
         ("my-MM-NilarNeural", "+25%", "tight", sample_my, "9_nilar_+25_tight"),
         ("my-MM-ThihaNeural", "+25%", "tight", sample_my, "10_thiha_+25_tight"),
         ("th-TH-PremwadeeNeural", "+15%", "tight", sample_th, "7_th_premwadee_tight"),
         ("google-th", "+15%", "tight", sample_th, "8_th_google_tight")]


def _analyse(path):
    """duration, number of pauses >= 0.3 s inside the speech, longest such pause (ffmpeg silencedetect)."""
    import re
    import subprocess
    import imageio_ffmpeg
    r = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-i", path, "-af", "silencedetect=n=-45dB:d=0.3", "-f", "null", "-"],
                       capture_output=True, text=True, errors="ignore")
    dur = 0.0
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", r.stderr)
    if m:
        dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", r.stderr)]
    lens = [float(x) for x in re.findall(r"silence_duration: ([\d.]+)", r.stderr)]
    inner = [ln for st, ln in zip(starts, lens) if st > 0.05 and st + ln < dur - 0.05]
    return dur, len(inner), (max(inner) if inner else 0.0)


async def run_voices():
    out(f"  {'ไฟล์':<24}{'ความยาว':>9}{'ช่วงหยุด≥0.3s':>15}{'หยุดนานสุด':>12}")
    for voice, rate, pace, text, name in tests:
        t0 = time.time()
        try:
            data = await main._speak_once(text, voice, rate, "+0Hz", pace)
            p = os.path.join(SAMPLE_DIR, name + ".mp3")
            open(p, "wb").write(data)
            dur, n, longest = _analyse(p)
            mark("ok", f"{name:<22}{dur:6.1f} วิ{n:>10} ครั้ง{longest:>9.2f} วิ   ({int((time.time() - t0) * 1000)} ms)")
        except Exception as e:
            mark("fail", f"{name:<22}{voice}: {str(e)[:120]}",
                 "Microsoft: ลอง .venv\\Scripts\\pip install -U edge-tts · Google: อาจถูกจำกัดชั่วคราว ระบบจะสลับไปอีกบริการให้เอง")

try:
    asyncio.run(run_voices())
    out("  วิธีเลือก: ฟังคู่ 1↔2 และ 4↔5 (ต้นฉบับ ↔ กระชับ) แล้วเทียบ Nilar / Thiha / Google")
    out("  ถ้ายังรู้สึก 'ยาน': ไม่ใช่เพราะช่วงหยุด (วัดได้ 0 ครั้ง) แต่เป็นความเร็วพูดของเสียงนั้น — ลอง 9 (Nilar +25%) และ 10 (Thiha +25%)")
    out(f"  ไฟล์อยู่ที่ {SAMPLE_DIR} — ชอบแบบไหน ตั้งใน Admin → ค่าเริ่มต้น (ผู้พูด / ความเร็ว / จังหวะพูด)")
except Exception:
    mark("fail", "ทดสอบเสียงไม่สำเร็จ: " + traceback.format_exc(limit=2)[-300:])

# ---------------------------------------------------------------- Summary
section("สรุป")
out(f"  ผ่าน {results['ok']} · เตือน {results['warn']} · ล้มเหลว {results['fail']}")
if results["fail"] == 0:
    out("  พร้อมทดสอบหน้าเว็บ: รัน 2_start.bat")
else:
    out("  แก้ข้อ [FAIL] ก่อน แล้วรัน 4_diagnose.bat ใหม่")
out(f"  รายงานเต็ม: {os.path.join(LOG_DIR, 'diagnose_report.txt')}")
open(os.path.join(LOG_DIR, "diagnose_report.txt"), "w", encoding="utf-8").write("\n".join(lines))
sys.exit(1 if results["fail"] else 0)
