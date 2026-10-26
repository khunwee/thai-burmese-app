"""
Thai -> Burmese Announcer (วุ้นแปลภาษาพม่า) — V19
FastAPI + PostgreSQL (Supabase) — deploy on Render free tier.

V19: browser fallback — on a free host every key-less translation service rate-limits the shared IP.
When the server cannot translate, the user's browser translates (from the user's own IP) and sends the
Burmese text back; the server still applies its cache / admin corrections and speaks it.
V16: "tight" speaking pace — silences longer than ~0.3 s are shortened so the voice sounds like a
person talking instead of a slow, drawn-out reading (measured with ffmpeg silencedetect).
V15: translation goes through translators.py — several FREE providers with automatic
fallback + cool-down, and a per-line translation cache, so one rate-limited service
("Too many requests") no longer stops an announcement.

Key principles of V14
- Data lives ONLY in PostgreSQL (Supabase). There is deliberately NO silent
  local/SQLite fallback: if DATABASE_URL is missing or wrong, the app says so
  loudly instead of quietly storing users in a file that disappears on restart.
- /api/system shows exactly which database the running server is connected to,
  so "users disappear after reboot" can be diagnosed in seconds.
"""
import asyncio
import base64
import datetime as dt
import hashlib
import hmac
import logging
import os
import re
import secrets
import subprocess
import tempfile
import threading
import time
import uuid
from contextlib import asynccontextmanager
from typing import Callable, Optional
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse

import jwt
import psycopg2
import psycopg2.pool
from psycopg2.extras import RealDictCursor, execute_values
from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

import translators as tr

APP_VERSION = "19.0"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
log = logging.getLogger("announcer")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


# ---------------------------------------------------------------------------
# .env support for local runs (no extra dependency). Render uses real env vars.
# ---------------------------------------------------------------------------
def _load_dotenv(path: str):
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            os.environ.setdefault(k, v)


_load_dotenv(os.path.join(BASE_DIR, ".env"))

TOKEN_HOURS = int(os.getenv("TOKEN_HOURS", "24"))
MAX_TEXT_LEN = int(os.getenv("MAX_TEXT_LEN", "3000"))
AUDIO_TTL_SECONDS = int(os.getenv("AUDIO_TTL_SECONDS", str(3 * 3600)))
HISTORY_KEEP_PER_USER = int(os.getenv("HISTORY_KEEP_PER_USER", "300"))
AUDIO_DIR = os.path.join(tempfile.gettempdir(), "tb_audio")
os.makedirs(AUDIO_DIR, exist_ok=True)

ALLOWED_ROLES = {"user", "admin"}
ALLOWED_ENGINES = tr.ENGINE_CHOICES
ALLOWED_FORMATS = {"mp3": "audio/mpeg", "wav": "audio/wav", "ogg": "audio/ogg"}
# V15: "google-*" = Google Translate voice (free); my-MM/th-TH = Microsoft Edge neural voices (free).
# th-TH-AcharaNeural was removed by Microsoft (no longer in the voice list) -> mapped to Premwadee.
BURMESE_VOICES = {"my-MM-NilarNeural", "my-MM-ThihaNeural", "google-my"}
THAI_VOICES = {"th-TH-PremwadeeNeural", "th-TH-NiwatNeural", "google-th"}
LEGACY_VOICES = {"th-TH-AcharaNeural": "th-TH-PremwadeeNeural"}
VOICE_FALLBACK = {"my-MM-NilarNeural": "google-my", "my-MM-ThihaNeural": "google-my", "google-my": "my-MM-NilarNeural",
                  "th-TH-PremwadeeNeural": "google-th", "th-TH-NiwatNeural": "google-th", "google-th": "th-TH-PremwadeeNeural"}
LANG_MODES = {"my", "th_my", "my_th"}  # Burmese only / Thai then Burmese / Burmese then Thai
RATE_RE = re.compile(r"^[+-]\d{1,3}%$")
PITCH_RE = re.compile(r"^[+-]\d{1,3}Hz$")
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.\-฀-๿]{3,32}$")

DEFAULT_SETTINGS = {
    "default_engine": "auto",
    "default_rate": "+15%",
    "default_pitch": "+0Hz",
    "default_voice": "my-MM-NilarNeural",
    "default_format": "mp3",
    "default_lang_mode": "my",
    "default_thai_voice": "th-TH-PremwadeeNeural",
    "default_repeat": "1",
    "default_pace": "tight",
}
PACE_MODES = {"tight", "original"}  # tight = shorten long pauses (sounds more like a person)


def _validate_setting(key: str, value: str) -> bool:
    checks = {
        "default_engine": lambda v: v in ALLOWED_ENGINES,
        "default_rate": lambda v: bool(RATE_RE.match(v)),
        "default_pitch": lambda v: bool(PITCH_RE.match(v)),
        "default_voice": lambda v: v in BURMESE_VOICES,
        "default_format": lambda v: v in ALLOWED_FORMATS,
        "default_lang_mode": lambda v: v in LANG_MODES,
        "default_thai_voice": lambda v: v in THAI_VOICES,
        "default_repeat": lambda v: v.isdigit() and 1 <= int(v) <= 5,
        "default_pace": lambda v: v in PACE_MODES,
    }
    return key in checks and checks[key](value)


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
class DBUnavailable(Exception):
    pass


def _normalize_db_url(url: Optional[str]) -> Optional[str]:
    """Fix common copy/paste mistakes and add safe defaults (SSL + timeout)."""
    if not url:
        return None
    url = url.strip().strip('"').strip("'")
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    # Passwords with @ # / ? : etc. break URL parsing -> percent-encode them automatically.
    m = re.match(r"^(postgresql://)([^:/@]+):(.*)@([^@]+)$", url)
    if m and not re.search(r"%[0-9A-Fa-f]{2}", m.group(3)):
        url = m.group(1) + m.group(2) + ":" + quote(m.group(3), safe="") + "@" + m.group(4)
    p = urlparse(url)
    q = dict(parse_qsl(p.query))
    host = (p.hostname or "").lower()
    is_local = host in ("", "localhost", "127.0.0.1") or "host" in q
    if not is_local:
        q.setdefault("sslmode", "require")
    q.setdefault("connect_timeout", "10")
    return urlunparse(p._replace(query=urlencode(q)))


DATABASE_URL = _normalize_db_url(os.getenv("DATABASE_URL"))


def describe_db() -> dict:
    """Human-readable description of which database this server talks to."""
    if not DATABASE_URL:
        return {"configured": False, "kind": "none", "host": None, "database": None, "user": None, "port": None}
    p = urlparse(DATABASE_URL)
    host = (p.hostname or dict(parse_qsl(p.query)).get("host", "")).lower()
    if "supabase" in host:
        kind = "supabase"
    elif "render.com" in host or host.startswith("dpg-"):
        kind = "render-postgres"
    elif host in ("localhost", "127.0.0.1") or host.startswith("/"):
        kind = "local"
    else:
        kind = "postgres"
    user = p.username or ""
    masked_user = (user[:12] + "…") if len(user) > 12 else user
    return {"configured": True, "kind": kind, "host": host, "port": p.port,
            "database": (p.path or "/").lstrip("/"), "user": masked_user}


_pool: Optional[psycopg2.pool.ThreadedConnectionPool] = None
_pool_lock = threading.Lock()
_db_ready = False
_db_last_error: Optional[str] = None


def placeholder_db_url(url: Optional[str]) -> Optional[str]:
    """Return a Thai explanation if the URL is still the example/template value, else None."""
    u = (url or "").upper()
    if "YOUR-PASSWORD" in u or "YOUR_PASSWORD" in u or "%5BYOUR" in u:
        return "DATABASE_URL ยังมีคำว่า YOUR_PASSWORD — ต้องแทนด้วยรหัสผ่านจริงของฐานข้อมูล Supabase"
    if "XXXXXXXX" in u:
        return "DATABASE_URL ยังเป็นค่าตัวอย่าง (postgres.xxxxxxxx) — คัดลอก connection string จริงจาก Supabase → Connect → Session pooler"
    return None


def explain_db_error(err) -> str:
    """Turn a psycopg2/Supabase connection error into a Thai cause + fix."""
    m = str(err).lower()
    if "tenant" in m and "not found" in m:
        return ("Supabase หาโปรเจกต์ไม่เจอ — ชื่อผู้ใช้ในลิงก์ (postgres.<รหัสโปรเจกต์>) ผิด/ยังเป็นค่าตัวอย่าง "
                "หรือโปรเจกต์ถูก Pause: คัดลอกลิงก์ใหม่จาก Supabase → Connect → Session pooler")
    if "password authentication failed" in m:
        return "รหัสผ่านฐานข้อมูลผิด — Supabase → Project Settings → Database → Reset database password แล้วใส่รหัสใหม่"
    if "could not translate host" in m or "getaddrinfo" in m or "name or service not known" in m or "nodename nor servname" in m:
        return "หาเซิร์ฟเวอร์ฐานข้อมูลไม่เจอ — ตรวจตัวสะกด host ในลิงก์ หรืออินเทอร์เน็ต"
    if "network is unreachable" in m or ("db." in m and "supabase.co" in m):
        return "ใช้ Direct connection (IPv6) ไม่ได้ — เปลี่ยนเป็น Session pooler (host ลงท้าย pooler.supabase.com)"
    if "timeout" in m or "timed out" in m or "connection refused" in m:
        return ("เชื่อมต่อไม่ทัน/ถูกปฏิเสธ — Firewall หรือเน็ตบริษัทอาจบล็อกพอร์ต 5432: "
                "ลองพอร์ต 6543 (Transaction pooler) หรือทดสอบผ่าน hotspot มือถือ")
    if "too many" in m and "connection" in m or "max client" in m:
        return "การเชื่อมต่อฐานข้อมูลเต็ม — รอสักครู่แล้วลองใหม่ (หรือปิดโปรแกรมที่ค้างอยู่)"
    if "ssl" in m:
        return "ปัญหา SSL — ตรวจว่าลิงก์ไม่ได้ตั้ง sslmode=disable"
    return "เชื่อมต่อฐานข้อมูลไม่ได้ — ตรวจ DATABASE_URL / Supabase ถูก Pause หรือไม่"


def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool, _db_last_error
    if not DATABASE_URL:
        raise DBUnavailable("ยังไม่ได้ตั้งค่า DATABASE_URL — ระบบจะไม่บันทึกข้อมูลใด ๆ จนกว่าจะตั้งค่า")
    ph = placeholder_db_url(DATABASE_URL)
    if ph:
        raise DBUnavailable(ph)
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                try:
                    _pool = psycopg2.pool.ThreadedConnectionPool(1, 8, DATABASE_URL, cursor_factory=RealDictCursor)
                except Exception as e:
                    _db_last_error = explain_db_error(e)
                    log.error("DB connect failed: %s", str(e).strip()[:300])
                    raise DBUnavailable(_db_last_error)
    return _pool


_sem = threading.BoundedSemaphore(8)  # never ask the pool for more than it has


def with_db(fn: Callable, autocommit: bool = False):
    """Run fn(cursor) inside a transaction. Retries once on a dropped connection
    (Supabase pooler closes idle connections; Render free sleeps).
    autocommit=True is for ONE-statement work (a read, or a single INSERT/UPDATE): it skips the
    separate COMMIT round-trip — each round-trip to Supabase costs ~100 ms from an office network."""
    global _db_last_error
    for attempt in (1, 2):
        pool = _get_pool()
        with _sem:
            conn = None
            try:
                conn = pool.getconn()
                if conn.closed:
                    raise psycopg2.InterfaceError("connection already closed")
                if autocommit:
                    conn.autocommit = True
                    try:
                        with conn.cursor() as cur:
                            result = fn(cur)
                    finally:
                        if not conn.closed:
                            conn.autocommit = False
                else:
                    with conn.cursor() as cur:
                        result = fn(cur)
                    conn.commit()
                pool.putconn(conn)
                return result
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                _db_last_error = str(e).strip()
                log.warning("DB connection problem (attempt %s): %s", attempt, _db_last_error)
                if conn is not None:
                    try:
                        pool.putconn(conn, close=True)
                    except Exception:
                        pass
                if attempt == 2:
                    raise DBUnavailable("ฐานข้อมูลไม่ตอบสนอง กรุณาลองใหม่อีกครั้ง")
            except Exception:
                if conn is not None:
                    try:
                        conn.rollback()
                        pool.putconn(conn)
                    except Exception:
                        pass
                raise


def init_db(attempts: int = 3) -> bool:
    global _db_ready, _db_last_error

    def _init(c):
        c.execute("""CREATE TABLE IF NOT EXISTS users (
                        id SERIAL PRIMARY KEY,
                        username VARCHAR(255) UNIQUE NOT NULL,
                        password_hash VARCHAR(255) NOT NULL,
                        role VARCHAR(50) NOT NULL)""")
        # V14 migrations (safe to run every start; keeps V13 data intact)
        for ddl in (
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS display_name VARCHAR(255)",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS must_change_password BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS token_version INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_login TIMESTAMPTZ",
        ):
            c.execute(ddl)
        c.execute("""CREATE TABLE IF NOT EXISTS settings (
                        key VARCHAR(255) PRIMARY KEY,
                        value VARCHAR(255) NOT NULL)""")
        c.execute("ALTER TABLE settings ALTER COLUMN value TYPE TEXT")
        c.execute("""CREATE TABLE IF NOT EXISTS phrases (
                        id SERIAL PRIMARY KEY,
                        category VARCHAR(100) NOT NULL DEFAULT 'ทั่วไป',
                        thai_text TEXT NOT NULL,
                        burmese_text TEXT,
                        sort_order INTEGER NOT NULL DEFAULT 0,
                        created_by VARCHAR(255),
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        c.execute("""CREATE TABLE IF NOT EXISTS history (
                        id BIGSERIAL PRIMARY KEY,
                        username VARCHAR(255) NOT NULL,
                        thai_text TEXT NOT NULL,
                        burmese_text TEXT NOT NULL,
                        engine VARCHAR(50),
                        voice VARCHAR(100),
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_history_user ON history(username, id DESC)")
        c.execute("""CREATE TABLE IF NOT EXISTS translations (
                        text_hash CHAR(64) PRIMARY KEY,
                        thai_text TEXT NOT NULL,
                        burmese_text TEXT NOT NULL,
                        engine VARCHAR(50) NOT NULL,
                        hits INTEGER NOT NULL DEFAULT 0,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")

        for k, v in DEFAULT_SETTINGS.items():
            c.execute("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING", (k, v))
        # Persistent JWT secret (only used when JWT_SECRET env is not set)
        c.execute("INSERT INTO settings (key, value) VALUES ('_jwt_secret', %s) ON CONFLICT (key) DO NOTHING",
                  (secrets.token_urlsafe(48),))

        # Bootstrap an admin ONLY when no admin exists at all.
        c.execute("SELECT COUNT(*) AS n FROM users WHERE role='admin' AND is_active")
        if c.fetchone()["n"] == 0:
            init_pw = os.getenv("ADMIN_PASSWORD") or "123456"
            c.execute("SELECT id FROM users WHERE username='admin'")
            row = c.fetchone()
            if row:
                c.execute("UPDATE users SET role='admin', is_active=TRUE WHERE id=%s", (row["id"],))
            else:
                c.execute("""INSERT INTO users (username, password_hash, role, must_change_password)
                             VALUES ('admin', %s, 'admin', %s)""",
                          (hash_password(init_pw), init_pw == "123456"))
                log.warning("Created bootstrap admin account 'admin' (change the password after first login)")

    if not DATABASE_URL:
        log.error("=" * 70)
        log.error("DATABASE_URL is NOT set. Users/settings cannot be saved.")
        log.error("Set DATABASE_URL to your Supabase connection string (see README).")
        log.error("=" * 70)
        return False
    for attempt in range(1, attempts + 1):
        try:
            with_db(_init)
            _db_ready = True
            info = describe_db()
            log.info("Database ready: kind=%s host=%s db=%s", info["kind"], info["host"], info["database"])
            if info["kind"] == "render-postgres":
                log.warning("Render free PostgreSQL is deleted after its free period — use Supabase for permanent data.")
            return True
        except Exception as e:
            _db_last_error = str(e) if isinstance(e, DBUnavailable) else "เชื่อมต่อฐานข้อมูลไม่ได้: " + str(e).strip()[:200]
            log.error("init_db attempt %s failed: %s", attempt, e)
            if attempt < attempts:
                time.sleep(2 * attempt)
    return False


_last_lazy_try = 0.0


def ensure_db():
    """Lazy init: if the DB was down at boot, initialise on a later request (throttled)."""
    global _last_lazy_try
    if not _db_ready:
        if time.time() - _last_lazy_try < 5:
            raise DBUnavailable(_db_last_error or "ยังไม่ได้ตั้งค่า DATABASE_URL")
        _last_lazy_try = time.time()
        if not init_db(attempts=1):
            raise DBUnavailable(_db_last_error or "ยังไม่ได้ตั้งค่า DATABASE_URL")


# ---------------------------------------------------------------------------
# Passwords & tokens
# ---------------------------------------------------------------------------
PBKDF2_ITER = 200_000


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, PBKDF2_ITER)
    return "pbkdf2_sha256$%d$%s$%s" % (PBKDF2_ITER, base64.b64encode(salt).decode(), base64.b64encode(dk).decode())


def verify_password(pw: str, stored: str) -> bool:
    if stored.startswith("pbkdf2_sha256$"):
        try:
            _, it, salt, dk = stored.split("$")
            calc = hashlib.pbkdf2_hmac("sha256", pw.encode(), base64.b64decode(salt), int(it))
            return hmac.compare_digest(calc, base64.b64decode(dk))
        except Exception:
            return False
    # Legacy V13 hash: unsalted sha256 hex — still accepted, upgraded on login.
    return hmac.compare_digest(hashlib.sha256(pw.encode()).hexdigest(), stored)


_jwt_secret_cache: Optional[str] = None


def jwt_secret() -> str:
    global _jwt_secret_cache
    env = os.getenv("JWT_SECRET")
    if env and len(env) >= 32 and env != "super-secret-key-for-jwt":
        return env
    if _jwt_secret_cache is None:
        ensure_db()
        row = with_db(lambda c: (c.execute("SELECT value FROM settings WHERE key='_jwt_secret'"), c.fetchone())[1])
        _jwt_secret_cache = row["value"]
    return _jwt_secret_cache


def make_token(u: dict) -> str:
    payload = {"sub": u["username"], "uid": u["id"], "role": u["role"], "tv": u["token_version"],
               "exp": dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=TOKEN_HOURS)}
    return jwt.encode(payload, jwt_secret(), algorithm="HS256")


def current_user(request: Request) -> dict:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "กรุณาเข้าสู่ระบบ")
    try:
        payload = jwt.decode(auth[7:], jwt_secret(), algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Session หมดอายุ กรุณาเข้าสู่ระบบใหม่")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Token ไม่ถูกต้อง กรุณาเข้าสู่ระบบใหม่")
    # Always re-check the user in the DB: deleted / disabled / password-reset users are kicked out,
    # and role changes take effect immediately.
    row = with_db(lambda c: (c.execute(
        "SELECT id, username, role, is_active, token_version, must_change_password FROM users WHERE id=%s",
        (payload.get("uid"),)), c.fetchone())[1], autocommit=True)
    if not row or not row["is_active"] or row["token_version"] != payload.get("tv") or row["username"] != payload.get("sub"):
        raise HTTPException(401, "บัญชีนี้ถูกเปลี่ยนแปลงหรือปิดใช้งาน กรุณาเข้าสู่ระบบใหม่")
    return dict(row)


def require_admin(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(403, "เฉพาะผู้ดูแลระบบ (Admin) เท่านั้น")
    return user


# Simple in-memory brute-force protection for /login
_fail: dict = {}
_FAIL_LIMIT, _FAIL_WINDOW = 5, 300


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "?")


def _check_rate(key: str):
    now = time.time()
    hits = [t for t in _fail.get(key, []) if now - t < _FAIL_WINDOW]
    _fail[key] = hits
    if len(hits) >= _FAIL_LIMIT:
        wait = int(_FAIL_WINDOW - (now - hits[0])) + 1
        raise HTTPException(429, f"ใส่รหัสผิดหลายครั้งเกินไป กรุณารอ {wait} วินาที")


# ---------------------------------------------------------------------------
# Translation & speech
# ---------------------------------------------------------------------------
def normalize_text(t: str) -> str:
    return re.sub(r"[ \t]+", " ", t.replace("\r\n", "\n")).strip()


def text_hash(t: str) -> str:
    return hashlib.sha256(t.encode()).hexdigest()


def _lines(text: str):
    return [ln.strip() for ln in text.split("\n") if ln.strip()]


async def translate(text: str, engine: str, last_resort: bool = True) -> tuple:
    """Returns (burmese, used_label, provider_name). Falls back across free providers."""
    try:
        my, name, notes = await asyncio.wait_for(run_in_threadpool(tr.translate, text, engine, last_resort),
                                                 timeout=tr.TOTAL_BUDGET + 20)
    except asyncio.TimeoutError:
        raise HTTPException(504, "บริการแปลภาษาตอบช้าเกินไป กรุณาลองใหม่อีกครั้ง",
                            headers={"X-Translate-Failed": "all"})
    except tr.AllProvidersFailed as e:
        # the header tells the page it may translate in the browser instead
        raise HTTPException(503, str(e), headers={"X-Translate-Failed": "all"})
    except tr.ProviderError as e:
        raise HTTPException(502, "แปลภาษาไม่สำเร็จ: " + str(e))
    label = tr.PROVIDERS[name].label
    if engine in tr.PROVIDERS and engine != name:
        label = f"{label} (สำรอง — {tr.PROVIDERS[engine].label} ใช้ไม่ได้ขณะนี้)"
    elif engine == "auto" and notes:
        label = f"{label} (อัตโนมัติ, ข้าม {len(notes)} ช่องทาง)"
    return my, label, name


class SpeechError(Exception):
    pass


async def _edge_tts(text: str, voice: str, rate: str, pitch: str) -> bytes:
    import edge_tts
    last = None
    for attempt in (1, 2):
        try:
            buf = bytearray()
            comm = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch)

            async def collect():
                async for chunk in comm.stream():
                    if chunk["type"] == "audio":
                        buf.extend(chunk["data"])
            await asyncio.wait_for(collect(), timeout=60)
            if buf:
                return bytes(buf)
            last = "ไม่ได้รับข้อมูลเสียง"
        except Exception as e:
            last = str(e)
            log.warning("edge-tts attempt %s failed: %s", attempt, e)
        await asyncio.sleep(0.8)
    raise SpeechError(f"Microsoft: {last}"[:200])


def _tts_chunks(text: str, limit: int = 180):
    """Google's voice endpoint takes ~200 chars per request: split at sentence marks, then spaces."""
    out = []
    for sent in re.split(r"(?<=[။၊.!?\n])", text):
        sent = sent.strip()
        if not sent:
            continue
        if out and len(out[-1]) + 1 + len(sent) <= limit:
            out[-1] += " " + sent
            continue
        while len(sent) > limit:
            cut = sent.rfind(" ", 0, limit)
            cut = cut if cut > limit // 3 else limit
            out.append(sent[:cut].strip())
            sent = sent[cut:].strip()
        if sent:
            out.append(sent)
    return out


def _google_tts_sync(text: str, lang: str) -> bytes:
    import requests
    parts = []
    chunks = _tts_chunks(text)
    for i, chunk in enumerate(chunks):
        if i:
            time.sleep(0.15)
        try:
            r = requests.get("https://translate.google.com/translate_tts", timeout=15,
                             params={"ie": "UTF-8", "client": "tw-ob", "tl": lang, "q": chunk,
                                     "total": len(chunks), "idx": i, "textlen": len(chunk)},
                             headers={"User-Agent": tr.UA, "Referer": "https://translate.google.com/"})
        except requests.RequestException as e:
            raise SpeechError("Google: เชื่อมต่อไม่ได้ " + str(e)[:100])
        if r.status_code == 429:
            raise SpeechError("Google: ถูกจำกัดความถี่ (429)")
        if r.status_code != 200 or "audio" not in r.headers.get("content-type", "") or len(r.content) < 200:
            raise SpeechError(f"Google: สร้างเสียงไม่ได้ (HTTP {r.status_code})")
        parts.append(r.content)
    return b"".join(parts)


# Pauses longer than ~0.25 s are cut to ~0.3 s; short pauses (normal rhythm) and the voice are untouched.
# Leading silence is removed and the tail is trimmed. Tested: 0.9 s gap -> 0.32 s, 0.15 s gap unchanged.
TIGHTEN_FILTER = ("silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.05:"
                  "stop_periods=-1:stop_duration=0.25:stop_threshold=-45dB:stop_silence=0.05")


def _ffmpeg_mp3(data: bytes, tempo: float = 1.0, tighten: bool = False) -> bytes:
    """Re-encode to the same format edge-tts uses (24 kHz mono 48 kbps MP3) so parts can be joined,
    and change speed with atempo (keeps the natural pitch, unlike playing the file faster)."""
    import imageio_ffmpeg
    args = [imageio_ffmpeg.get_ffmpeg_exe(), "-loglevel", "error", "-f", "mp3", "-i", "pipe:0"]
    filters = []
    if tighten:
        filters.append(TIGHTEN_FILTER)
    if abs(tempo - 1.0) > 0.01:
        filters.append(f"atempo={tempo:.3f}")
    if filters:
        args += ["-filter:a", ",".join(filters)]
    # no ID3/Xing header: pieces are joined frame-by-frame, a header in the middle would be a glitch
    args += ["-ar", "24000", "-ac", "1", "-b:a", "48k", "-map_metadata", "-1", "-id3v2_version", "0",
             "-write_xing", "0", "-f", "mp3", "pipe:1"]
    r = subprocess.run(args, input=data, capture_output=True, timeout=90)
    if r.returncode != 0 or not r.stdout:
        raise SpeechError("แปลงเสียงไม่สำเร็จ: " + r.stderr.decode(errors="ignore")[-150:])
    return r.stdout


_silence_cache: dict = {}


def _silence(ms: int = 350) -> bytes:
    if ms not in _silence_cache:
        import imageio_ffmpeg
        r = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-loglevel", "error", "-f", "lavfi", "-i",
                            "anullsrc=r=24000:cl=mono", "-t", f"{ms / 1000:.2f}", "-ar", "24000", "-ac", "1",
                            "-b:a", "48k", "-map_metadata", "-1", "-id3v2_version", "0", "-write_xing", "0",
                            "-f", "mp3", "pipe:1"], capture_output=True, timeout=30)
        _silence_cache[ms] = r.stdout if r.returncode == 0 else b""
    return _silence_cache[ms]


def _rate_to_tempo(rate: str) -> float:
    try:
        return max(0.5, min(2.0, 1 + int(rate.rstrip("%")) / 100))
    except ValueError:
        return 1.0


async def _speak_once(text: str, voice: str, rate: str, pitch: str, pace: str = "tight") -> bytes:
    tight = pace == "tight"
    if voice.startswith("google-"):
        raw = await run_in_threadpool(_google_tts_sync, text, voice.split("-", 1)[1])
        return await run_in_threadpool(_ffmpeg_mp3, raw, _rate_to_tempo(rate), tight)
    data = await _edge_tts(text, voice, rate, pitch)
    if tight:
        try:
            data = await run_in_threadpool(_ffmpeg_mp3, data, 1.0, True)
        except Exception as e:  # never lose the announcement because of the pause filter
            log.warning("pause tightening skipped: %s", e)
    return data


async def tts_bytes(text: str, voice: str, rate: str, pitch: str, pace: str = "tight") -> tuple:
    """Speak with the chosen voice; if that service fails, use the other free voice service.
    Returns (mp3_bytes, voice_actually_used)."""
    voice = LEGACY_VOICES.get(voice, voice)
    errors = []
    for v in (voice, VOICE_FALLBACK.get(voice)):
        if not v:
            continue
        try:
            return await _speak_once(text, v, rate, pitch, pace), v
        except SpeechError as e:
            errors.append(str(e))
            log.warning("TTS voice %s failed: %s", v, e)
        except Exception as e:
            errors.append(str(e)[:150])
            log.warning("TTS voice %s crashed: %s", v, e)
    raise HTTPException(502, "สร้างเสียงไม่สำเร็จ — " + " · ".join(errors))


def _cleanup_audio():
    now = time.time()
    try:
        for name in os.listdir(AUDIO_DIR):
            p = os.path.join(AUDIO_DIR, name)
            if now - os.path.getmtime(p) > AUDIO_TTL_SECONDS:
                os.remove(p)
    except Exception:
        pass


def save_audio(data: bytes) -> str:
    _cleanup_audio()
    aid = uuid.uuid4().hex
    with open(os.path.join(AUDIO_DIR, f"{aid}.mp3"), "wb") as f:
        f.write(data)
    return aid


def _convert(src: str, dst: str):
    import imageio_ffmpeg
    tmp = dst + ".part" + os.path.splitext(dst)[1]
    r = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-i", src, tmp],
                       capture_output=True, timeout=90)
    if r.returncode != 0 or not os.path.exists(tmp):
        raise RuntimeError(r.stderr.decode(errors="ignore")[-300:])
    os.replace(tmp, dst)


async def audio_path(aid: str, fmt: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{32}", aid or "") or fmt not in ALLOWED_FORMATS:
        raise HTTPException(400, "คำขอไฟล์เสียงไม่ถูกต้อง")
    src = os.path.join(AUDIO_DIR, f"{aid}.mp3")
    if not os.path.exists(src):
        raise HTTPException(404, "ไฟล์เสียงหมดอายุแล้ว กรุณากดสร้างเสียงใหม่")
    if fmt == "mp3":
        return src
    dst = os.path.join(AUDIO_DIR, f"{aid}.{fmt}")
    if not os.path.exists(dst):
        try:
            await run_in_threadpool(_convert, src, dst)
        except Exception as e:
            raise HTTPException(500, f"แปลงไฟล์เสียงไม่สำเร็จ: {e}")
    return dst


def _check_voice_params(voice: str, rate: str, pitch: str):
    if voice not in BURMESE_VOICES:
        raise HTTPException(400, "เสียงผู้พูดไม่ถูกต้อง")
    if not RATE_RE.match(rate) or not PITCH_RE.match(pitch):
        raise HTTPException(400, "ค่าความเร็ว/โทนเสียงไม่ถูกต้อง")


async def build_speech(thai: str, burmese: str, voice: str, rate: str, pitch: str,
                       lang_mode: str, thai_voice: str, pace: str = "tight") -> tuple:
    """Returns (audio_id, note). note says if a backup voice service had to be used."""
    if lang_mode not in LANG_MODES:
        lang_mode = "my"
    thai_voice = LEGACY_VOICES.get(thai_voice, thai_voice)
    if thai_voice not in THAI_VOICES:
        thai_voice = DEFAULT_SETTINGS["default_thai_voice"]
    note = []
    if pace not in PACE_MODES:
        pace = "tight"
    my_audio, used = await tts_bytes(burmese, voice, rate, pitch, pace)
    if used != voice:
        note.append("เสียงพม่าใช้ระบบสำรอง")
    if lang_mode == "my" or not thai:
        return save_audio(my_audio), " ".join(note)
    th_audio, used_th = await tts_bytes(thai, thai_voice, rate, "+0Hz", pace)
    if used_th != thai_voice:
        note.append("เสียงไทยใช้ระบบสำรอง")
    # All parts are 24 kHz mono 48 kbps MP3 (Google audio is re-encoded to match), so they can be joined;
    # a short pause between the two languages sounds more like a person speaking.
    gap = _silence(400)
    data = th_audio + gap + my_audio if lang_mode == "th_my" else my_audio + gap + th_audio
    return save_audio(data), " ".join(note)


def record_history(username: str, thai: str, burmese: str, engine: str, voice: str):
    """Runs in the background after the response is sent (the user never waits for it).
    One INSERT; trimming old rows only every 25th entry keeps it to a single round-trip."""
    def _ins(c):
        c.execute("""INSERT INTO history (username, thai_text, burmese_text, engine, voice)
                     VALUES (%s,%s,%s,%s,%s) RETURNING id""", (username, thai, burmese, engine, voice))
        return c.fetchone()["id"]

    def _prune(c):
        c.execute("""DELETE FROM history WHERE username=%s AND id NOT IN
                     (SELECT id FROM history WHERE username=%s ORDER BY id DESC LIMIT %s)""",
                  (username, username, HISTORY_KEEP_PER_USER))
    try:
        new_id = with_db(_ins, autocommit=True)
        if new_id % 25 == 0:
            with_db(_prune, autocommit=True)
    except Exception as e:  # history must never break announcing
        log.warning("history insert failed: %s", e)


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    await run_in_threadpool(init_db)
    yield
    if _pool:
        _pool.closeall()


app = FastAPI(title="Thai-Burmese Announcer", version=APP_VERSION, lifespan=lifespan)


@app.exception_handler(DBUnavailable)
async def _db_unavailable(request: Request, exc: DBUnavailable):
    return JSONResponse(status_code=503, content={"detail": str(exc)})


@app.middleware("http")
async def _lazy_db(request: Request, call_next):
    if request.url.path.startswith("/api") or request.url.path in ("/login", "/process"):
        try:
            await run_in_threadpool(ensure_db)
        except DBUnavailable as e:
            return JSONResponse(status_code=503, content={"detail": str(e)})
    return await call_next(request)


@app.get("/", response_class=HTMLResponse)
def home():
    with open(os.path.join(BASE_DIR, "index.html"), encoding="utf-8") as f:
        return HTMLResponse(f.read(), headers={"Cache-Control": "no-cache"})


@app.api_route("/healthz", methods=["GET", "HEAD"])
def healthz():
    """Use with UptimeRobot / cron-job.org every 5–10 min: keeps Render awake and
    keeps Supabase from pausing the free project (it runs a real query)."""
    info = describe_db()
    db_ok, hint = False, None
    if info["configured"]:
        try:
            with_db(lambda c: c.execute("SELECT 1"), autocommit=True)
            db_ok = True
        except DBUnavailable as e:
            hint = str(e)  # a Thai cause + fix, never the URL or password
        except Exception as e:
            hint = explain_db_error(e)
    return {"ok": True, "version": APP_VERSION, "db_configured": info["configured"],
            "db_ok": db_ok, "db_kind": info["kind"], "db_hint": hint}


# ---- Auth -----------------------------------------------------------------
@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    username = username.strip()
    key = f"{_client_ip(request)}|{username.lower()}"
    _check_rate(key)
    row = with_db(lambda c: (c.execute("SELECT * FROM users WHERE username=%s", (username,)), c.fetchone())[1],
                  autocommit=True)
    if not row or not verify_password(password, row["password_hash"]):
        _fail.setdefault(key, []).append(time.time())
        raise HTTPException(401, "ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง")
    if not row["is_active"]:
        raise HTTPException(403, "บัญชีนี้ถูกปิดใช้งาน กรุณาติดต่อผู้ดูแล")
    _fail.pop(key, None)
    if password == "123456" and not row["must_change_password"]:
        row["must_change_password"] = True
        with_db(lambda c: c.execute("UPDATE users SET must_change_password=TRUE WHERE id=%s", (row["id"],)))
    new_hash = None if row["password_hash"].startswith("pbkdf2_") else hash_password(password)

    def _upd(c):
        if new_hash:  # silently upgrade legacy V13 hash
            c.execute("UPDATE users SET password_hash=%s WHERE id=%s", (new_hash, row["id"]))
        c.execute("UPDATE users SET last_login=NOW() WHERE id=%s", (row["id"],))
    with_db(_upd)
    return {"access_token": make_token(row), "role": row["role"], "username": row["username"],
            "display_name": row.get("display_name") or row["username"],
            "must_change_password": row["must_change_password"]}


@app.get("/api/me")
def me(user: dict = Depends(current_user)):
    return {"username": user["username"], "role": user["role"], "must_change_password": user["must_change_password"]}


@app.post("/api/me/password")
def change_my_password(old_password: str = Form(...), new_password: str = Form(...),
                       user: dict = Depends(current_user)):
    if len(new_password) < 6:
        raise HTTPException(400, "รหัสผ่านใหม่ต้องยาวอย่างน้อย 6 ตัวอักษร")
    if new_password == "123456":
        raise HTTPException(400, "ห้ามใช้รหัสผ่านเริ่มต้น 123456")
    row = with_db(lambda c: (c.execute("SELECT * FROM users WHERE id=%s", (user["id"],)), c.fetchone())[1])
    if not verify_password(old_password, row["password_hash"]):
        raise HTTPException(400, "รหัสผ่านเดิมไม่ถูกต้อง")

    def _upd(c):
        c.execute("""UPDATE users SET password_hash=%s, must_change_password=FALSE,
                     token_version=token_version+1 WHERE id=%s RETURNING *""",
                  (hash_password(new_password), user["id"]))
        return c.fetchone()
    updated = with_db(_upd)
    return {"status": "success", "access_token": make_token(updated)}


# ---- Users (admin) ---------------------------------------------------------
@app.get("/api/users")
def list_users(admin: dict = Depends(require_admin)):
    rows = with_db(lambda c: (c.execute(
        """SELECT id, username, display_name, role, is_active, must_change_password, created_at, last_login
           FROM users ORDER BY role, username"""), c.fetchall())[1])
    return [dict(r) for r in rows]


@app.post("/api/users")
def create_user(username: str = Form(...), password: str = Form(...), role: str = Form("user"),
                display_name: str = Form(""), admin: dict = Depends(require_admin)):
    username = username.strip()
    if not USERNAME_RE.match(username):
        raise HTTPException(400, "ชื่อผู้ใช้ต้องยาว 3–32 ตัว ใช้ได้ ก-ฮ a-z 0-9 _ . - (ไม่มีช่องว่าง)")
    if len(password) < 4:
        raise HTTPException(400, "รหัสผ่านต้องยาวอย่างน้อย 4 ตัวอักษร")
    if role not in ALLOWED_ROLES:
        raise HTTPException(400, "สิทธิ์ไม่ถูกต้อง")

    def _ins(c):
        c.execute("SELECT 1 FROM users WHERE LOWER(username)=LOWER(%s)", (username,))
        if c.fetchone():
            raise HTTPException(400, "มีชื่อผู้ใช้งานนี้อยู่ในระบบแล้ว")
        c.execute("""INSERT INTO users (username, password_hash, role, display_name)
                     VALUES (%s,%s,%s,%s) RETURNING id""",
                  (username, hash_password(password), role, display_name.strip()[:100] or None))
        return c.fetchone()["id"]
    new_id = with_db(_ins)
    # Read back to PROVE it was persisted
    total = with_db(lambda c: (c.execute("SELECT COUNT(*) AS n FROM users"), c.fetchone())[1])["n"]
    log.info("User '%s' created by %s (total users in DB: %s)", username, admin["username"], total)
    return {"status": "success", "id": new_id, "total_users": total}


def _active_admins(c) -> int:
    c.execute("SELECT COUNT(*) AS n FROM users WHERE role='admin' AND is_active")
    return c.fetchone()["n"]


@app.patch("/api/users/{user_id}")
def update_user(user_id: int, role: Optional[str] = Form(None), is_active_raw: Optional[str] = Form(None, alias="is_active"),
                new_password: Optional[str] = Form(None), display_name: Optional[str] = Form(None),
                admin: dict = Depends(require_admin)):
    # V15 fix: newer FastAPI turns a *missing* bool form field into False, which disabled the user
    # whenever an admin only reset a password or changed a role. Parse it ourselves instead.
    is_active = None if is_active_raw in (None, "") else str(is_active_raw).lower() in ("true", "1", "on", "yes")
    def _upd(c):
        c.execute("SELECT * FROM users WHERE id=%s", (user_id,))
        t = c.fetchone()
        if not t:
            raise HTTPException(404, "ไม่พบผู้ใช้")
        demoting = (role is not None and role != "admin") or is_active is False
        if t["role"] == "admin" and t["is_active"] and demoting and _active_admins(c) <= 1:
            raise HTTPException(400, "ต้องมี Admin ที่ใช้งานได้อย่างน้อย 1 คน")
        if t["id"] == admin["id"] and demoting:
            raise HTTPException(400, "ไม่สามารถลดสิทธิ์หรือปิดบัญชีของตัวเองได้")
        bump = False
        if role is not None:
            if role not in ALLOWED_ROLES:
                raise HTTPException(400, "สิทธิ์ไม่ถูกต้อง")
            c.execute("UPDATE users SET role=%s WHERE id=%s", (role, user_id))
        if is_active is not None:
            c.execute("UPDATE users SET is_active=%s WHERE id=%s", (is_active, user_id))
            bump = bump or not is_active
        if display_name is not None:
            c.execute("UPDATE users SET display_name=%s WHERE id=%s", (display_name.strip()[:100] or None, user_id))
        if new_password:
            if len(new_password) < 4:
                raise HTTPException(400, "รหัสผ่านต้องยาวอย่างน้อย 4 ตัวอักษร")
            c.execute("UPDATE users SET password_hash=%s, must_change_password=TRUE WHERE id=%s",
                      (hash_password(new_password), user_id))
            bump = True
        if bump:  # log the user out everywhere
            c.execute("UPDATE users SET token_version=token_version+1 WHERE id=%s", (user_id,))
    with_db(_upd)
    return {"status": "success"}


@app.delete("/api/users/{user_id}")
def delete_user(user_id: int, admin: dict = Depends(require_admin)):
    def _del(c):
        c.execute("SELECT * FROM users WHERE id=%s", (user_id,))
        t = c.fetchone()
        if not t:
            raise HTTPException(404, "ไม่พบผู้ใช้")
        if t["id"] == admin["id"]:
            raise HTTPException(400, "ไม่สามารถลบบัญชีของตัวเองได้")
        if t["role"] == "admin" and t["is_active"] and _active_admins(c) <= 1:
            raise HTTPException(400, "ต้องมี Admin ที่ใช้งานได้อย่างน้อย 1 คน")
        c.execute("DELETE FROM users WHERE id=%s", (user_id,))
    with_db(_del)
    return {"status": "success"}


# ---- Settings ---------------------------------------------------------------
@app.get("/api/settings")
def get_settings(user: dict = Depends(current_user)):
    rows = with_db(lambda c: (c.execute("SELECT key, value FROM settings WHERE LEFT(key, 1) <> '_'"), c.fetchall())[1])
    out = dict(DEFAULT_SETTINGS)
    out.update({r["key"]: r["value"] for r in rows})
    return out


@app.post("/api/settings")
async def update_settings(request: Request, admin: dict = Depends(require_admin)):
    form = await request.form()
    data = {k: str(v) for k, v in form.items() if k in DEFAULT_SETTINGS}
    bad = [k for k, v in data.items() if not _validate_setting(k, v)]
    if bad:
        raise HTTPException(400, f"ค่าที่ตั้งไม่ถูกต้อง: {', '.join(bad)}")

    def _up(c):
        for k, v in data.items():
            c.execute("""INSERT INTO settings (key, value) VALUES (%s,%s)
                         ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value""", (k, v))
    await run_in_threadpool(with_db, _up)
    return {"status": "success", "saved": list(data)}


# ---- System status (admin) — the "where is my data?" screen -------------------
@app.get("/api/system")
def system_status(admin: dict = Depends(require_admin)):
    info = describe_db()

    def _q(c):
        c.execute("SELECT version() AS v, current_database() AS d, NOW() AS now")
        meta = c.fetchone()
        counts = {}
        for t in ("users", "phrases", "history", "translations"):
            c.execute(f"SELECT COUNT(*) AS n FROM {t}")
            counts[t] = c.fetchone()["n"]
        c.execute("SELECT COUNT(*) AS n FROM users WHERE must_change_password AND role='admin'")
        weak_admin = c.fetchone()["n"]
        return meta, counts, weak_admin
    meta, counts, weak_admin = with_db(_q)
    warnings = []
    if info["kind"] == "render-postgres":
        warnings.append("กำลังใช้ Render PostgreSQL ฟรี — ฐานข้อมูลจะถูกลบเมื่อหมดช่วงฟรี ควรย้ายไป Supabase")
    if info["kind"] == "local":
        warnings.append("กำลังใช้ฐานข้อมูลในเครื่อง (localhost) — ถ้ารันบน Render ข้อมูลจะไม่ถาวร")
    if info["kind"] == "supabase" and info["port"] == 5432 and "pooler" not in (info["host"] or ""):
        warnings.append("ใช้ Supabase Direct connection (IPv6) — บน Render แนะนำใช้ Session/Transaction pooler แทน")
    env_secret = os.getenv("JWT_SECRET")
    if not env_secret or len(env_secret) < 32:
        warnings.append("ยังไม่ได้ตั้ง JWT_SECRET (ยาว ≥32 ตัว) — ระบบใช้ secret ที่สุ่มเก็บไว้ในฐานข้อมูลแทน (ปลอดภัย แต่แนะนำตั้งใน Render)")
    if not (os.getenv("GAS_URL") or os.getenv("GEMINI_API_KEY")):
        warnings.append("ยังไม่มีช่องทางแปลที่ผูกบัญชี (GAS_URL หรือ GEMINI_API_KEY) — บน host ฟรี Google/MyMemory แบบไม่ใช้คีย์ "
                        "มักถูกจำกัดเพราะใช้ IP ร่วมกัน ระหว่างนี้ระบบให้เบราว์เซอร์ผู้ใช้แปลแทนอัตโนมัติ")
    if not os.getenv("MYMEMORY_EMAIL"):
        warnings.append("ยังไม่ได้ตั้ง MYMEMORY_EMAIL — MyMemory แปลได้แค่ 5,000 ตัวอักษร/วัน (ใส่อีเมลเพิ่มเป็น 50,000)")
    if weak_admin:
        warnings.append("ยังมี Admin ที่ใช้รหัสผ่านเริ่มต้นอยู่ — กรุณาเปลี่ยนรหัสผ่าน")
    return {"app_version": APP_VERSION, "db": info, "db_server": meta["v"].split(" on ")[0],
            "db_time": meta["now"], "counts": counts, "warnings": warnings,
            "render_instance": os.getenv("RENDER_INSTANCE_ID"), "render_commit": os.getenv("RENDER_GIT_COMMIT"),
            "server_started": SERVER_STARTED, "providers": tr.status()}


SERVER_STARTED = dt.datetime.now(dt.timezone.utc).isoformat()


# ---- Phrases (shared quick announcements) ------------------------------------
@app.get("/api/phrases")
def list_phrases(user: dict = Depends(current_user)):
    rows = with_db(lambda c: (c.execute(
        "SELECT id, category, thai_text, burmese_text, sort_order FROM phrases ORDER BY category, sort_order, id"),
        c.fetchall())[1])
    return [dict(r) for r in rows]


def _phrase_args(category: str, thai_text: str, burmese_text: str):
    thai_text = normalize_text(thai_text)
    if not thai_text:
        raise HTTPException(400, "กรุณากรอกข้อความภาษาไทย")
    if len(thai_text) > MAX_TEXT_LEN:
        raise HTTPException(400, f"ข้อความยาวเกิน {MAX_TEXT_LEN} ตัวอักษร")
    return (category.strip()[:100] or "ทั่วไป"), thai_text, (normalize_text(burmese_text) or None)


@app.post("/api/phrases")
def create_phrase(category: str = Form("ทั่วไป"), thai_text: str = Form(...), burmese_text: str = Form(""),
                  sort_order: int = Form(0), admin: dict = Depends(require_admin)):
    cat, th, my = _phrase_args(category, thai_text, burmese_text)
    pid = with_db(lambda c: (c.execute(
        """INSERT INTO phrases (category, thai_text, burmese_text, sort_order, created_by)
           VALUES (%s,%s,%s,%s,%s) RETURNING id""", (cat, th, my, sort_order, admin["username"])),
        c.fetchone())[1])["id"]
    return {"status": "success", "id": pid}


@app.put("/api/phrases/{pid}")
def update_phrase(pid: int, category: str = Form("ทั่วไป"), thai_text: str = Form(...),
                  burmese_text: str = Form(""), sort_order: int = Form(0), admin: dict = Depends(require_admin)):
    cat, th, my = _phrase_args(category, thai_text, burmese_text)
    n = with_db(lambda c: (c.execute(
        """UPDATE phrases SET category=%s, thai_text=%s, burmese_text=%s, sort_order=%s, updated_at=NOW()
           WHERE id=%s""", (cat, th, my, sort_order, pid)), c.rowcount)[1])
    if not n:
        raise HTTPException(404, "ไม่พบข้อความ")
    return {"status": "success"}


@app.delete("/api/phrases/{pid}")
def delete_phrase(pid: int, admin: dict = Depends(require_admin)):
    with_db(lambda c: c.execute("DELETE FROM phrases WHERE id=%s", (pid,)))
    return {"status": "success"}


# ---- History -----------------------------------------------------------------
@app.get("/api/history")
def get_history(limit: int = 50, all_users: bool = False, user: dict = Depends(current_user)):
    limit = max(1, min(limit, 300))
    if all_users and user["role"] == "admin":
        sql, args = "SELECT * FROM history ORDER BY id DESC LIMIT %s", (limit,)
    else:
        sql, args = "SELECT * FROM history WHERE username=%s ORDER BY id DESC LIMIT %s", (user["username"], limit)
    rows = with_db(lambda c: (c.execute(sql, args), c.fetchall())[1])
    return [dict(r) for r in rows]


@app.delete("/api/history")
def clear_history(user: dict = Depends(current_user)):
    with_db(lambda c: c.execute("DELETE FROM history WHERE username=%s", (user["username"],)))
    return {"status": "success"}


# ---- Translate + speak ---------------------------------------------------------
def _cache_get(th: str):
    return with_db(lambda c: (c.execute(
        "UPDATE translations SET hits=hits+1 WHERE text_hash=%s RETURNING burmese_text, engine",
        (text_hash(th),)), c.fetchone())[1], autocommit=True)


def _cache_get_many(lines: list) -> dict:
    """Per-line cache lookup -> {thai_line: burmese_line} (manual corrections included)."""
    if not lines:
        return {}
    hashes = {text_hash(ln): ln for ln in lines}

    def _q(c):
        c.execute("UPDATE translations SET hits=hits+1 WHERE text_hash = ANY(%s) RETURNING text_hash, burmese_text",
                  (list(hashes),))
        return c.fetchall()
    try:
        return {hashes[r["text_hash"]]: r["burmese_text"] for r in with_db(_q, autocommit=True)}
    except Exception as e:
        log.warning("line cache read failed: %s", e)
        return {}


def _cache_put(th: str, my: str, engine: str, manual: bool = False):
    def _p(c):
        if manual:
            c.execute("""INSERT INTO translations (text_hash, thai_text, burmese_text, engine)
                         VALUES (%s,%s,%s,'manual')
                         ON CONFLICT (text_hash) DO UPDATE SET burmese_text=EXCLUDED.burmese_text,
                         engine='manual', updated_at=NOW()""", (text_hash(th), th, my))
        else:  # never overwrite a human-corrected translation
            c.execute("""INSERT INTO translations (text_hash, thai_text, burmese_text, engine)
                         VALUES (%s,%s,%s,%s)
                         ON CONFLICT (text_hash) DO UPDATE SET burmese_text=EXCLUDED.burmese_text,
                         engine=EXCLUDED.engine, updated_at=NOW() WHERE translations.engine <> 'manual'""",
                      (text_hash(th), th, my, engine))
    try:
        with_db(_p)
    except Exception as e:
        log.warning("translation cache write failed: %s", e)


CLIENT_ENGINES = {"browser-google": "Google (แปลผ่านเบราว์เซอร์)", "browser-google2": "Google สำรอง (แปลผ่านเบราว์เซอร์)",
                  "browser-mymemory": "MyMemory (แปลผ่านเบราว์เซอร์)"}


async def cache_lookup(th: str):
    """Cached Burmese for this Thai text (admin corrections first), or None. No translation call."""
    lines = _lines(th)
    multi = len(lines) > 1
    hit = await run_in_threadpool(_cache_get, th)
    if hit and (hit["engine"] == "manual" or not multi):
        return hit["burmese_text"], ("คำแปลที่แก้ไขแล้ว" if hit["engine"] == "manual" else f"{hit['engine']} (จำไว้)")
    if multi:
        known = await run_in_threadpool(_cache_get_many, lines)
        if all(ln in known for ln in lines):
            return "\n".join(known[ln] for ln in lines), "จำไว้ (ทีละบรรทัด)"
    if hit:
        return hit["burmese_text"], f"{hit['engine']} (จำไว้)"
    return None


def _save_client_translation(th: str, my: str, engine: str):
    """Remember a translation the browser made, per line when the lines line up (never over admin corrections)."""
    lines, out_lines = _lines(th), _lines(my)
    items = list(zip(lines, out_lines)) if len(lines) > 1 and len(lines) == len(out_lines) else [(th, my)]
    _cache_put_many([(a, b, engine) for a, b in items])


async def translate_cached(th: str, engine: str, fresh: bool = False, last_resort: bool = True) -> tuple:
    """Whole-text cache -> per-line cache -> translate only the missing lines (one request).
    Fewer outgoing requests = far fewer "Too many requests" errors."""
    lines = _lines(th)
    multi = len(lines) > 1
    hit = None if fresh else await run_in_threadpool(_cache_get, th)
    # A whole-text entry wins if an admin corrected it, or for single-line text.
    if hit and (hit["engine"] == "manual" or not multi):
        label = "คำแปลที่แก้ไขแล้ว" if hit["engine"] == "manual" else f"{hit['engine']} (จำไว้)"
        return hit["burmese_text"], label, True
    # Multi-line text is rebuilt from per-line entries, so a correction of one line is always applied.
    known = {} if fresh or not multi else await run_in_threadpool(_cache_get_many, lines)
    missing = list(dict.fromkeys(ln for ln in lines if ln not in known))
    if not missing:
        return "\n".join(known[ln] for ln in lines), "จำไว้ (ทีละบรรทัด)", True
    if hit:  # stored whole (e.g. from V14, or lines could not be mapped back)
        return hit["burmese_text"], f"{hit['engine']} (จำไว้)", True
    out, used, name = await translate("\n".join(missing), engine, last_resort)
    out_lines = _lines(out)
    to_save = []
    if len(out_lines) == len(missing):
        new = dict(zip(missing, out_lines))
        if len(missing) > 1 or len(lines) > 1:
            to_save += [(t_line, m_line, name) for t_line, m_line in new.items()]
        known.update(new)
        my = "\n".join(known[ln] for ln in lines)
    elif not known:
        my = out  # provider merged/split lines: keep its whole output
    else:  # cannot map lines back -> translate the whole text once
        my, used, name = await translate(th, engine, last_resort)
    if known and len(missing) < len(lines):
        used += f" + จำไว้ {len(lines) - len(missing)} บรรทัด"
    if not multi or len(out_lines) != len(missing):  # multi-line text that mapped cleanly lives in per-line entries
        to_save.append((th, my, name))
    await run_in_threadpool(_cache_put_many, to_save)  # one round-trip for all lines
    return my, used, False


def _cache_put_many(items: list):
    """Save several (thai, burmese, engine) machine translations in ONE statement.
    Never overwrites a human-corrected ('manual') translation."""
    rows = {}
    for th, my, engine in items:
        if th and my:
            rows[text_hash(th)] = (text_hash(th), th, my, engine)
    if not rows:
        return

    def _p(c):
        execute_values(c, """INSERT INTO translations (text_hash, thai_text, burmese_text, engine) VALUES %s
                             ON CONFLICT (text_hash) DO UPDATE SET burmese_text=EXCLUDED.burmese_text,
                             engine=EXCLUDED.engine, updated_at=NOW() WHERE translations.engine <> 'manual'""",
                       list(rows.values()))
    try:
        with_db(_p, autocommit=True)
    except Exception as e:
        log.warning("translation cache write failed: %s", e)


def _speech_response(aid, my: str, engine: str, cached: bool, fmt: str):
    aid, note = aid if isinstance(aid, tuple) else (aid, "")
    if note:
        engine = f"{engine} · {note}"
    return {"burmese_text": my, "engine_used": engine, "cached": cached, "audio_id": aid,
            "play_url": f"/audio/{aid}?fmt=mp3", "download_url": f"/download/{aid}?fmt={fmt}"}


@app.post("/process")
async def process_text(text: str = Form(...), rate: str = Form("+15%"), pitch: str = Form("+0Hz"),
                       voice: str = Form("my-MM-NilarNeural"), audio_format: str = Form("mp3"),
                       engine: str = Form("auto"), lang_mode: str = Form("my"),
                       thai_voice: str = Form("th-TH-PremwadeeNeural"), fresh: bool = Form(False),
                       pace: str = Form("tight"), client_fallback: str = Form(""),
                       background: BackgroundTasks = None, user: dict = Depends(current_user)):
    th = normalize_text(text)
    if not th:
        raise HTTPException(400, "กรุณาพิมพ์ข้อความ")
    if len(th) > MAX_TEXT_LEN:
        raise HTTPException(400, f"ข้อความยาวเกิน {MAX_TEXT_LEN} ตัวอักษร")
    if engine not in ALLOWED_ENGINES or audio_format not in ALLOWED_FORMATS:
        raise HTTPException(400, "ตัวเลือกไม่ถูกต้อง")
    _check_voice_params(voice, rate, pitch)

    # client_fallback=1: the page can translate in the browser, so fail fast instead of re-trying
    my, used, cached = await translate_cached(th, engine, fresh, last_resort=client_fallback != "1")

    aid = await build_speech(th, my, voice, rate, pitch, lang_mode, thai_voice, pace)
    background.add_task(record_history, user["username"], th, my, used, voice)
    return _speech_response(aid, my, used, cached, audio_format)


@app.post("/api/tts")
async def speak_burmese(burmese_text: str = Form(...), thai_text: str = Form(""), rate: str = Form("+15%"),
                        pitch: str = Form("+0Hz"), voice: str = Form("my-MM-NilarNeural"),
                        audio_format: str = Form("mp3"), lang_mode: str = Form("my"),
                        thai_voice: str = Form("th-TH-PremwadeeNeural"), save_correction: bool = Form(False),
                        pace: str = Form("tight"), client_engine: str = Form(""),
                        background: BackgroundTasks = None, user: dict = Depends(current_user)):
    """Speak a given Burmese text (edited translation, a saved phrase, or a translation the browser made)."""
    my, th = normalize_text(burmese_text), normalize_text(thai_text)
    if not my:
        raise HTTPException(400, "ไม่มีข้อความภาษาพม่า")
    if len(my) > MAX_TEXT_LEN or len(th) > MAX_TEXT_LEN:
        raise HTTPException(400, "ข้อความยาวเกินไป")
    if audio_format not in ALLOWED_FORMATS:
        raise HTTPException(400, "ฟอร์แมตไม่ถูกต้อง")
    _check_voice_params(voice, rate, pitch)
    if save_correction:
        if user["role"] != "admin":
            raise HTTPException(403, "เฉพาะ Admin ที่บันทึกคำแปลถาวรได้")
        if not th:
            raise HTTPException(400, "ต้องมีข้อความภาษาไทยต้นฉบับเพื่อบันทึกคำแปล")
        await run_in_threadpool(_cache_put, th, my, "manual", True)
    label, cached = ("แก้ไขเอง" if save_correction else "tts"), False
    if client_engine and not save_correction:
        if client_engine not in CLIENT_ENGINES or not th:
            raise HTTPException(400, "ข้อมูลคำแปลจากเบราว์เซอร์ไม่ถูกต้อง")
        hit = await cache_lookup(th)  # an admin correction / remembered translation still wins
        if hit:
            my, label = hit
            cached = True
        else:
            label = CLIENT_ENGINES[client_engine]
            background.add_task(_save_client_translation, th, my, client_engine)
    aid = await build_speech(th, my, voice, rate, pitch, lang_mode, thai_voice, pace)
    if th:
        hist = "manual" if save_correction else (label if client_engine else "tts")
        background.add_task(record_history, user["username"], th, my, hist[:50], voice)
    return _speech_response(aid, my, label, cached, audio_format)


@app.post("/api/translate")
async def translate_only(text: str = Form(...), engine: str = Form("auto"), admin: dict = Depends(require_admin)):
    """Used by the phrase editor to pre-fill the Burmese translation."""
    th = normalize_text(text)
    if not th or engine not in ALLOWED_ENGINES:
        raise HTTPException(400, "ข้อมูลไม่ถูกต้อง")
    my, used, _cached = await translate_cached(th, engine)
    return {"burmese_text": my, "engine_used": used}


# ---- Translation providers ---------------------------------------------------------
@app.get("/api/engines")
def list_engines(user: dict = Depends(current_user)):
    return [{"name": p["name"], "label": p["label"], "configured": p["configured"],
             "cooldown_seconds": p["cooldown_seconds"]} for p in tr.status()]


@app.post("/api/engines/test")
async def test_engines(admin: dict = Depends(require_admin)):
    return await run_in_threadpool(tr.test_all)


@app.post("/api/engines/reset")
def reset_engines(admin: dict = Depends(require_admin)):
    tr.reset_cooldowns()
    return {"status": "success"}


# ---- Audio files ----------------------------------------------------------------
# Audio IDs are random 128-bit values, so links can't be guessed; <audio> tags can't send headers.
@app.get("/audio/{aid}")
async def get_audio(aid: str, fmt: str = "mp3"):
    path = await audio_path(aid, fmt)
    return FileResponse(path, media_type=ALLOWED_FORMATS[fmt], headers={"Cache-Control": "private, max-age=3600"})


@app.get("/download/{aid}")
async def download_audio(aid: str, fmt: str = "mp3"):
    path = await audio_path(aid, fmt)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    return FileResponse(path, media_type=ALLOWED_FORMATS[fmt], filename=f"burmese_audio_{stamp}.{fmt}")
