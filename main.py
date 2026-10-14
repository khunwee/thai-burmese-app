"""
Thai -> Burmese Announcer (วุ้นแปลภาษาพม่า) — V14
FastAPI + PostgreSQL (Supabase) — deploy on Render free tier.

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
from psycopg2.extras import RealDictCursor
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

APP_VERSION = "14.0"
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
ALLOWED_ENGINES = {"auto", "google", "mymemory"}
ALLOWED_FORMATS = {"mp3": "audio/mpeg", "wav": "audio/wav", "ogg": "audio/ogg"}
BURMESE_VOICES = {"my-MM-NilarNeural", "my-MM-ThihaNeural"}
THAI_VOICES = {"th-TH-PremwadeeNeural", "th-TH-NiwatNeural", "th-TH-AcharaNeural"}
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
}


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


def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool, _db_last_error
    if not DATABASE_URL:
        raise DBUnavailable("ยังไม่ได้ตั้งค่า DATABASE_URL — ระบบจะไม่บันทึกข้อมูลใด ๆ จนกว่าจะตั้งค่า")
    if "YOUR-PASSWORD" in DATABASE_URL.upper():
        raise DBUnavailable("DATABASE_URL ยังมีคำว่า [YOUR-PASSWORD] อยู่ — ต้องแทนที่ด้วยรหัสผ่านจริงของ Supabase")
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                try:
                    _pool = psycopg2.pool.ThreadedConnectionPool(1, 8, DATABASE_URL, cursor_factory=RealDictCursor)
                except Exception as e:
                    _db_last_error = "เชื่อมต่อฐานข้อมูลไม่ได้: " + str(e).strip()[:200]
                    log.error("DB connect failed: %s", e)
                    raise DBUnavailable("เชื่อมต่อฐานข้อมูลไม่ได้ (ตรวจสอบ DATABASE_URL / Supabase ถูก Pause หรือไม่)")
    return _pool


_sem = threading.BoundedSemaphore(8)  # never ask the pool for more than it has


def with_db(fn: Callable):
    """Run fn(cursor) inside a transaction. Retries once on a dropped connection
    (Supabase pooler closes idle connections; Render free sleeps)."""
    global _db_last_error
    for attempt in (1, 2):
        pool = _get_pool()
        with _sem:
            conn = None
            try:
                conn = pool.getconn()
                if conn.closed:
                    raise psycopg2.InterfaceError("connection already closed")
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
        (payload.get("uid"),)), c.fetchone())[1])
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


def _chunks(text: str, limit: int):
    """Split text into pieces <= limit chars on line / space boundaries."""
    out = []
    for line in text.split("\n"):
        line = line.strip()
        while len(line) > limit:
            cut = line.rfind(" ", 0, limit)
            if cut <= 0:
                cut = limit
            out.append(line[:cut].strip())
            line = line[cut:].strip()
        if line:
            out.append(line)
    return out


def _translate_sync(text: str, engine: str) -> str:
    from deep_translator import GoogleTranslator, MyMemoryTranslator
    if engine == "google":
        tr, limit = GoogleTranslator(source="th", target="my"), 4500
    else:
        tr, limit = MyMemoryTranslator(source="th-TH", target="my-MM"), 450
    parts = [tr.translate(p) for p in _chunks(text, limit)]
    return "\n".join(p for p in parts if p)


async def translate(text: str, engine: str) -> tuple:
    async def run(e):
        return await asyncio.wait_for(run_in_threadpool(_translate_sync, text, e), timeout=25)

    if engine in ("google", "mymemory"):
        return await run(engine), engine
    try:
        res = await run("google")
        if res:
            return res, "google (auto)"
    except Exception as e:
        log.warning("Google translate failed, falling back to MyMemory: %s", e)
    return await run("mymemory"), "mymemory (fallback)"


async def tts_bytes(text: str, voice: str, rate: str, pitch: str) -> bytes:
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
    raise HTTPException(502, f"สร้างเสียงไม่สำเร็จ (edge-tts): {last}")


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
                       lang_mode: str, thai_voice: str) -> str:
    if lang_mode not in LANG_MODES:
        lang_mode = "my"
    if thai_voice not in THAI_VOICES:
        thai_voice = DEFAULT_SETTINGS["default_thai_voice"]
    my_audio = await tts_bytes(burmese, voice, rate, pitch)
    if lang_mode == "my" or not thai:
        return save_audio(my_audio)
    th_audio = await tts_bytes(thai, thai_voice, rate, "+0Hz")
    # Both streams are 24 kHz mono MP3 from edge-tts, so frame-level concatenation plays correctly.
    data = th_audio + my_audio if lang_mode == "th_my" else my_audio + th_audio
    return save_audio(data)


def record_history(username: str, thai: str, burmese: str, engine: str, voice: str):
    def _ins(c):
        c.execute("INSERT INTO history (username, thai_text, burmese_text, engine, voice) VALUES (%s,%s,%s,%s,%s)",
                  (username, thai, burmese, engine, voice))
        c.execute("""DELETE FROM history WHERE username=%s AND id NOT IN
                     (SELECT id FROM history WHERE username=%s ORDER BY id DESC LIMIT %s)""",
                  (username, username, HISTORY_KEEP_PER_USER))
    try:
        with_db(_ins)
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
    db_ok = False
    if info["configured"]:
        try:
            with_db(lambda c: c.execute("SELECT 1"))
            db_ok = True
        except Exception:
            db_ok = False
    return {"ok": True, "version": APP_VERSION, "db_configured": info["configured"],
            "db_ok": db_ok, "db_kind": info["kind"]}


# ---- Auth -----------------------------------------------------------------
@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    username = username.strip()
    key = f"{_client_ip(request)}|{username.lower()}"
    _check_rate(key)
    row = with_db(lambda c: (c.execute("SELECT * FROM users WHERE username=%s", (username,)), c.fetchone())[1])
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
def update_user(user_id: int, role: Optional[str] = Form(None), is_active: Optional[bool] = Form(None),
                new_password: Optional[str] = Form(None), display_name: Optional[str] = Form(None),
                admin: dict = Depends(require_admin)):
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
    if weak_admin:
        warnings.append("ยังมี Admin ที่ใช้รหัสผ่านเริ่มต้นอยู่ — กรุณาเปลี่ยนรหัสผ่าน")
    return {"app_version": APP_VERSION, "db": info, "db_server": meta["v"].split(" on ")[0],
            "db_time": meta["now"], "counts": counts, "warnings": warnings,
            "render_instance": os.getenv("RENDER_INSTANCE_ID"), "render_commit": os.getenv("RENDER_GIT_COMMIT"),
            "server_started": SERVER_STARTED}


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
        (text_hash(th),)), c.fetchone())[1])


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


def _speech_response(aid: str, my: str, engine: str, cached: bool, fmt: str):
    return {"burmese_text": my, "engine_used": engine, "cached": cached, "audio_id": aid,
            "play_url": f"/audio/{aid}?fmt=mp3", "download_url": f"/download/{aid}?fmt={fmt}"}


@app.post("/process")
async def process_text(text: str = Form(...), rate: str = Form("+15%"), pitch: str = Form("+0Hz"),
                       voice: str = Form("my-MM-NilarNeural"), audio_format: str = Form("mp3"),
                       engine: str = Form("auto"), lang_mode: str = Form("my"),
                       thai_voice: str = Form("th-TH-PremwadeeNeural"), fresh: bool = Form(False),
                       user: dict = Depends(current_user)):
    th = normalize_text(text)
    if not th:
        raise HTTPException(400, "กรุณาพิมพ์ข้อความ")
    if len(th) > MAX_TEXT_LEN:
        raise HTTPException(400, f"ข้อความยาวเกิน {MAX_TEXT_LEN} ตัวอักษร")
    if engine not in ALLOWED_ENGINES or audio_format not in ALLOWED_FORMATS:
        raise HTTPException(400, "ตัวเลือกไม่ถูกต้อง")
    _check_voice_params(voice, rate, pitch)

    cached = False
    hit = None if fresh else await run_in_threadpool(_cache_get, th)
    if hit:
        my, used = hit["burmese_text"], ("คำแปลที่แก้ไขแล้ว" if hit["engine"] == "manual" else f"{hit['engine']} (cache)")
        cached = True
    else:
        try:
            my, used = await translate(th, engine)
        except asyncio.TimeoutError:
            raise HTTPException(504, "บริการแปลภาษาตอบช้าเกินไป กรุณาลองใหม่ หรือเปลี่ยนช่องทางแปล")
        except Exception as e:
            raise HTTPException(502, f"แปลภาษาไม่สำเร็จ: {e}")
        if not my:
            raise HTTPException(502, "ไม่สามารถแปลภาษาได้ในขณะนี้ กรุณาลองใหม่")
        await run_in_threadpool(_cache_put, th, my, used.split(" ")[0])

    aid = await build_speech(th, my, voice, rate, pitch, lang_mode, thai_voice)
    await run_in_threadpool(record_history, user["username"], th, my, used, voice)
    return _speech_response(aid, my, used, cached, audio_format)


@app.post("/api/tts")
async def speak_burmese(burmese_text: str = Form(...), thai_text: str = Form(""), rate: str = Form("+15%"),
                        pitch: str = Form("+0Hz"), voice: str = Form("my-MM-NilarNeural"),
                        audio_format: str = Form("mp3"), lang_mode: str = Form("my"),
                        thai_voice: str = Form("th-TH-PremwadeeNeural"), save_correction: bool = Form(False),
                        user: dict = Depends(current_user)):
    """Speak a given Burmese text (edited translation or a saved phrase) — no translation call."""
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
    aid = await build_speech(th, my, voice, rate, pitch, lang_mode, thai_voice)
    if th:
        await run_in_threadpool(record_history, user["username"], th, my, "manual" if save_correction else "tts", voice)
    return _speech_response(aid, my, "แก้ไขเอง" if save_correction else "tts", False, audio_format)


@app.post("/api/translate")
async def translate_only(text: str = Form(...), engine: str = Form("auto"), admin: dict = Depends(require_admin)):
    """Used by the phrase editor to pre-fill the Burmese translation."""
    th = normalize_text(text)
    if not th or engine not in ALLOWED_ENGINES:
        raise HTTPException(400, "ข้อมูลไม่ถูกต้อง")
    try:
        my, used = await translate(th, engine)
    except Exception as e:
        raise HTTPException(502, f"แปลภาษาไม่สำเร็จ: {e}")
    return {"burmese_text": my, "engine_used": used}


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
