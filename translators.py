"""
translators.py — free Thai -> Burmese translation providers with automatic fallback (V15)

Why this exists
- V14 depended on two channels only (Google web scraping + anonymous MyMemory). On Render's
  shared outbound IPs those run out quickly: MyMemory gives an anonymous IP just 5,000 chars/day,
  so "Too many requests" (HTTP 429) showed up even for a single announcement.
- V15 has several FREE providers. If one is rate-limited, out of quota or down, the next one is
  tried automatically, and the failing one is put on a cool-down so it is not hammered again.

Providers (all free; the ones marked * need a free account/key from you):
  google      Google Translate (translate.googleapis.com, client=gtx)  — no key, long text in 1 POST
  google2     Google Translate (clients5.google.com, dict-chrome-ex)    — no key, separate host/limit
  googleweb   Google Translate web page via deep-translator (V14 method) — no key, last resort
  mymemory    MyMemory — 5,000 chars/day anonymous, 50,000/day with MYMEMORY_EMAIL
  gas       * Your own Google Apps Script web app (LanguageApp) — 5,000 calls/day per Google account,
              runs on Google's servers so it is NOT affected by Render's shared IP
  gemini    * Google Gemini API free tier (GEMINI_API_KEY from aistudio.google.com)
  cloudflare* Cloudflare Workers AI m2m100 (CF_ACCOUNT_ID + CF_API_TOKEN), 10,000 neurons/day free
"""
import json
import logging
import os
import re
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

import requests

log = logging.getLogger("announcer.translate")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
HTTP_TIMEOUT = float(os.getenv("TRANSLATE_HTTP_TIMEOUT", "15"))
TOTAL_BUDGET = float(os.getenv("TRANSLATE_TOTAL_BUDGET", "45"))  # seconds for the whole fallback chain


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class ProviderError(Exception):
    """kind: quota | rate | auth | config | timeout | network | bad_output | error"""

    def __init__(self, message: str, kind: str = "error", retry_after: Optional[float] = None):
        super().__init__(message)
        self.kind = kind
        self.retry_after = retry_after


class AllProvidersFailed(Exception):
    def __init__(self, attempts: List[Tuple[str, str]]):
        self.attempts = attempts
        detail = " · ".join(f"{PROVIDERS[n].label}: {m}" for n, m in attempts) if attempts else \
            "ไม่มีช่องทางแปลที่ใช้งานได้"
        super().__init__("แปลภาษาไม่สำเร็จทุกช่องทาง — " + detail + " (ลองใหม่อีกครั้งในอีกสักครู่)")


KIND_TH = {
    "quota": "โควตาฟรีวันนี้หมดแล้ว",
    "rate": "ถูกจำกัดความถี่ชั่วคราว (ส่งคำขอถี่เกินไป)",
    "auth": "คีย์/รหัสลับไม่ถูกต้อง",
    "config": "ยังไม่ได้ตั้งค่า",
    "timeout": "ตอบช้าเกินไป",
    "network": "เชื่อมต่อไม่ได้",
    "bad_output": "ได้ผลแปลที่ใช้ไม่ได้",
    "error": "เกิดข้อผิดพลาด",
}


def _http_error(resp: requests.Response, who: str) -> ProviderError:
    code = resp.status_code
    retry = None
    ra = resp.headers.get("Retry-After")
    if ra and ra.isdigit():
        retry = float(ra)
    if code == 429:
        return ProviderError(f"{who}: HTTP 429 — {KIND_TH['rate']}", "rate", retry)
    if code in (401, 403):
        return ProviderError(f"{who}: HTTP {code} — ถูกปฏิเสธ/คีย์ไม่ถูกต้อง", "auth" if who in ("Gemini", "Cloudflare") else "rate")
    if code >= 500:
        return ProviderError(f"{who}: เซิร์ฟเวอร์ปลายทางขัดข้อง (HTTP {code})", "error")
    return ProviderError(f"{who}: HTTP {code}", "error")


def _get(url, **kw):
    try:
        return requests.get(url, timeout=HTTP_TIMEOUT, headers={"User-Agent": UA, **kw.pop("headers", {})}, **kw)
    except requests.Timeout:
        raise ProviderError("หมดเวลารอ", "timeout")
    except requests.RequestException as e:
        raise ProviderError("เชื่อมต่อไม่ได้: " + str(e)[:120], "network")


def _post(url, **kw):
    try:
        return requests.post(url, timeout=HTTP_TIMEOUT, headers={"User-Agent": UA, **kw.pop("headers", {})}, **kw)
    except requests.Timeout:
        raise ProviderError("หมดเวลารอ", "timeout")
    except requests.RequestException as e:
        raise ProviderError("เชื่อมต่อไม่ได้: " + str(e)[:120], "network")


# ---------------------------------------------------------------------------
# Chunking: pack whole lines together up to a provider's size limit
# (V14 sent one request per line -> many more requests than needed).
# ---------------------------------------------------------------------------
def _split_long(line: str, limit: int, measure: Callable[[str], int]) -> List[str]:
    out, cur = [], ""
    for word in re.split(r"(\s+)", line):
        if measure(cur + word) <= limit:
            cur += word
            continue
        if cur.strip():
            out.append(cur.strip())
        cur = word.lstrip()
        while measure(cur) > limit:  # a single giant "word": hard cut
            cut = len(cur)
            while cut > 1 and measure(cur[:cut]) > limit:
                cut = int(cut * 0.8)
            out.append(cur[:cut])
            cur = cur[cut:]
    if cur.strip():
        out.append(cur.strip())
    return out


def pack(text: str, limit: int, measure: Callable[[str], int] = len) -> List[str]:
    """Group lines into as few chunks as possible, each <= limit (line breaks kept inside chunks)."""
    chunks, cur = [], []
    for line in text.split("\n"):
        pieces = [line] if measure(line) <= limit else _split_long(line, limit, measure)
        for p in pieces:
            candidate = "\n".join(cur + [p])
            if cur and measure(candidate) > limit:
                chunks.append("\n".join(cur))
                cur = [p]
            else:
                cur.append(p)
    if cur and "\n".join(cur).strip():
        chunks.append("\n".join(cur))
    return chunks


def utf8_len(s: str) -> int:
    return len(s.encode("utf-8"))


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------
class Provider:
    name = ""
    label = ""
    note = ""            # shown in the admin status page
    limit = 4000         # max size of one request (measured by `measure`)
    measure = staticmethod(len)
    min_interval = 0.0   # minimum seconds between two requests to this provider (politeness)
    needs = ()           # env vars required

    def __init__(self):
        self._lock = threading.Lock()
        self._last = 0.0

    def configured(self) -> bool:
        return all(os.getenv(v) for v in self.needs)

    def _throttle(self):
        with self._lock:
            wait = self._last + self.min_interval - time.time()
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()

    def translate(self, text: str) -> str:
        parts = []
        for chunk in pack(text, self.limit, self.measure):
            self._throttle()
            parts.append(self._one(chunk).strip())
        out = "\n".join(p for p in parts if p)
        if not out.strip():
            raise ProviderError("ได้ผลแปลว่างเปล่า", "bad_output")
        return out

    def _one(self, text: str) -> str:
        raise NotImplementedError


class GoogleGtx(Provider):
    name, label = "google", "Google"
    note = "translate.googleapis.com (client=gtx) — ไม่ต้องใช้คีย์"
    limit, min_interval = 4500, 0.25

    def _one(self, text):
        r = _post("https://translate.googleapis.com/translate_a/single",
                  params={"client": "gtx", "sl": "th", "tl": "my", "dt": "t"},
                  data={"q": text},
                  headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"})
        if r.status_code != 200:
            raise _http_error(r, "Google")
        try:
            data = r.json()
            return "".join(seg[0] for seg in data[0] if seg and seg[0])
        except Exception:
            raise ProviderError("Google: รูปแบบคำตอบเปลี่ยนไป", "bad_output")


class GoogleDict(Provider):
    name, label = "google2", "Google (สำรอง)"
    note = "clients5.google.com (dict-chrome-ex) — ไม่ต้องใช้คีย์, คนละเซิร์ฟเวอร์กับ Google หลัก"
    limit, min_interval = 600, 0.25   # GET request: keep the URL short

    def _one(self, text):
        r = _get("https://clients5.google.com/translate_a/t",
                 params={"client": "dict-chrome-ex", "sl": "th", "tl": "my", "q": text})
        if r.status_code != 200:
            raise _http_error(r, "Google2")
        try:
            data = r.json()
            # formats seen: ["..."]  |  [["...","th"]]  |  {"sentences":[{"trans":...}]}
            if isinstance(data, dict):
                return "".join(s.get("trans", "") for s in data.get("sentences", []))
            first = data[0]
            return first[0] if isinstance(first, list) else str(first)
        except Exception:
            raise ProviderError("Google2: รูปแบบคำตอบเปลี่ยนไป", "bad_output")


class GoogleWeb(Provider):
    name, label = "googleweb", "Google (หน้าเว็บ)"
    note = "วิธีเดิมของ V14 ผ่าน deep-translator — ใช้เป็นทางสุดท้าย"
    limit, min_interval = 4500, 0.4

    def _one(self, text):
        try:
            from deep_translator import GoogleTranslator
            from deep_translator.exceptions import TooManyRequests
        except Exception:
            raise ProviderError("ไม่ได้ติดตั้ง deep-translator", "config")
        try:
            return GoogleTranslator(source="th", target="my").translate(text) or ""
        except TooManyRequests:
            raise ProviderError("Google (หน้าเว็บ): HTTP 429", "rate")
        except Exception as e:
            raise ProviderError("Google (หน้าเว็บ): " + str(e)[:120], "error")


class MyMemory(Provider):
    name, label = "mymemory", "MyMemory"
    limit, min_interval = 480, 0.5          # API max is 500 BYTES (Thai = 3 bytes/char)
    measure = staticmethod(utf8_len)

    @property
    def note(self):
        return ("ใส่อีเมลแล้ว — 50,000 ตัวอักษร/วัน" if os.getenv("MYMEMORY_EMAIL")
                else "ไม่ได้ใส่ MYMEMORY_EMAIL — แค่ 5,000 ตัวอักษร/วัน และใช้ร่วมกับทุกเว็บบน IP เดียวกันของ Render")

    def _one(self, text):
        params = {"q": text, "langpair": "th|my"}
        if os.getenv("MYMEMORY_EMAIL"):
            params["de"] = os.getenv("MYMEMORY_EMAIL")
        r = _get("https://api.mymemory.translated.net/get", params=params)
        body = {}
        try:
            body = r.json()
        except Exception:
            pass
        out = ((body.get("responseData") or {}).get("translatedText") or "")
        details = str(body.get("responseDetails") or "")
        status = str(body.get("responseStatus") or r.status_code)
        warn = (out + " " + details).upper()
        if "MYMEMORY WARNING" in warn or body.get("quotaFinished") or r.status_code == 429 or status == "429":
            m = re.search(r"NEXT AVAILABLE IN\s+(\d+)\s+HOURS?", warn)
            hours = int(m.group(1)) if m else 1
            raise ProviderError("MyMemory: โควตาฟรีวันนี้หมดแล้ว", "quota", max(600, hours * 3600))
        if "LIMIT EXCEEDED" in warn or "INVALID" in warn:
            raise ProviderError("MyMemory: " + details[:100], "error")
        if r.status_code != 200 or status not in ("200", "None"):
            raise _http_error(r, "MyMemory") if r.status_code != 200 else ProviderError("MyMemory: " + details[:100], "error")
        return out


GAS_ACCESS_HELP = ("Apps Script: Google ให้ล็อกอินก่อน (HTTP 401) — เปิด script.google.com → Deploy → Manage deployments → "
                   "ดินสอ (Edit) → Who has access: Anyone → Version: New version → Deploy (ใช้ URL /exec เดิม). "
                   "ถ้าไม่มีตัวเลือก Anyone แปลว่าบัญชีบริษัท (Workspace) ปิดไว้ — สร้างสคริปต์ด้วย Gmail ส่วนตัวแทน")


class AppsScript(Provider):
    name, label = "gas", "Google Apps Script (ของคุณเอง)"
    note = "ใช้ LanguageApp ในบัญชี Google ของคุณ — 5,000 ครั้ง/วัน ไม่ขึ้นกับ IP ของ Render"
    needs = ("GAS_URL",)
    limit, min_interval = 4500, 0.0

    def translate(self, text):
        chunks = pack(text, self.limit)
        payload = {"secret": os.getenv("GAS_SECRET", ""), "source": "th", "target": "my", "texts": chunks}
        url = os.getenv("GAS_URL", "").strip()
        if url.endswith("/dev"):
            raise ProviderError("Apps Script: ใช้ลิงก์ /dev (ทดสอบ) ซึ่งต้องล็อกอิน — ใช้ Web app URL ที่ลงท้าย /exec", "config")
        r = _post(url, data=json.dumps(payload),
                  headers={"Content-Type": "text/plain;charset=utf-8"}, allow_redirects=True)
        login_page = "accounts.google.com" in (r.url or "") or "ServiceLogin" in r.text[:3000]
        if r.status_code in (401, 403) or login_page:
            raise ProviderError(GAS_ACCESS_HELP, "config")
        if r.status_code == 404:
            raise ProviderError("Apps Script: ไม่พบ Web app (HTTP 404) — URL ผิด หรือ deployment ถูกลบ: Deploy → Manage deployments แล้วคัดลอก URL ใหม่", "config")
        if r.status_code == 429:
            raise ProviderError("Apps Script: ถูกจำกัดความถี่ชั่วคราว (429)", "rate")
        if r.status_code != 200:
            raise ProviderError(f"Apps Script: HTTP {r.status_code}", "error")
        try:
            body = r.json()
        except Exception:
            raise ProviderError("Apps Script: ตอบกลับไม่ใช่ JSON — " + GAS_ACCESS_HELP.split(" — ", 1)[1], "config")
        if not body.get("ok"):
            err = str(body.get("error", ""))
            if "unauthorized" in err:
                raise ProviderError("Apps Script: GAS_SECRET ไม่ตรงกัน", "auth")
            if "too many times" in err.lower():
                raise ProviderError("Apps Script: โควตา 5,000 ครั้ง/วันหมดแล้ว", "quota", 3 * 3600)
            raise ProviderError("Apps Script: " + err[:120], "error")
        out = "\n".join(str(t).strip() for t in body.get("translations", []) if str(t).strip())
        if not out:
            raise ProviderError("ได้ผลแปลว่างเปล่า", "bad_output")
        return out


class Gemini(Provider):
    name, label = "gemini", "Gemini AI"
    needs = ("GEMINI_API_KEY",)
    limit, min_interval = 6000, 1.0
    DEFAULT_MODELS = "gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemini-2.5-flash-lite,gemini-2.0-flash"

    @property
    def note(self):
        return "Google AI Studio free tier — รุ่นที่ลอง: " + self._models()[0] + " (เปลี่ยนได้ด้วย GEMINI_MODEL)"

    def _models(self):
        return [m.strip() for m in (os.getenv("GEMINI_MODEL") or self.DEFAULT_MODELS).split(",") if m.strip()]

    def _one(self, text):
        n = text.count("\n") + 1
        prompt = ("Translate the following Thai factory announcement into natural, polite Burmese "
                  "(Myanmar language, Unicode script). Keep exactly the same line breaks "
                  f"({n} line{'s' if n > 1 else ''}). Output ONLY the Burmese translation, nothing else.\n\n" + text)
        last = None
        for model in self._models():
            r = _post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                      headers={"x-goog-api-key": os.getenv("GEMINI_API_KEY"), "Content-Type": "application/json"},
                      data=json.dumps({"contents": [{"parts": [{"text": prompt}]}],
                                       "generationConfig": {"temperature": 0}}))
            if r.status_code == 404:
                last = ProviderError(f"Gemini: ไม่พบรุ่น {model}", "config")
                continue
            if r.status_code == 429:
                m = re.search(r'"retryDelay":\s*"(\d+)', r.text)
                raise ProviderError("Gemini: เกินโควตาฟรี (429)", "rate", float(m.group(1)) + 5 if m else 120)
            if r.status_code in (400, 401, 403) and "API key" in r.text:
                raise ProviderError("Gemini: API key ไม่ถูกต้อง", "auth")
            if r.status_code != 200:
                raise _http_error(r, "Gemini")
            try:
                parts = r.json()["candidates"][0]["content"]["parts"]
                return "".join(p.get("text", "") for p in parts).strip()
            except Exception:
                raise ProviderError("Gemini: ไม่ได้ผลแปล (อาจถูกกรองเนื้อหา)", "bad_output")
        raise last or ProviderError("Gemini: ไม่มีรุ่นที่ใช้ได้", "config")


class Cloudflare(Provider):
    name, label = "cloudflare", "Cloudflare AI (m2m100)"
    note = "Workers AI ฟรี 10,000 neurons/วัน — คุณภาพด้อยกว่า Google ใช้เป็นตัวสำรอง"
    needs = ("CF_ACCOUNT_ID", "CF_API_TOKEN")
    limit, min_interval = 500, 0.2

    def _one(self, text):
        acc = os.getenv("CF_ACCOUNT_ID")
        r = _post(f"https://api.cloudflare.com/client/v4/accounts/{acc}/ai/run/@cf/meta/m2m100-1.2b",
                  headers={"Authorization": "Bearer " + os.getenv("CF_API_TOKEN", ""), "Content-Type": "application/json"},
                  data=json.dumps({"text": text, "source_lang": "th", "target_lang": "my"}))
        if r.status_code == 429 or "neurons" in r.text.lower() and r.status_code != 200:
            raise ProviderError("Cloudflare: โควตาฟรีวันนี้หมดแล้ว", "quota", 3600)
        if r.status_code != 200:
            raise _http_error(r, "Cloudflare")
        try:
            return r.json()["result"]["translated_text"]
        except Exception:
            raise ProviderError("Cloudflare: รูปแบบคำตอบเปลี่ยนไป", "bad_output")


PROVIDERS: Dict[str, Provider] = {p.name: p for p in
                                  (AppsScript(), GoogleGtx(), GoogleDict(), Gemini(), MyMemory(), Cloudflare(), GoogleWeb())}
DEFAULT_ORDER = "gas,google,google2,gemini,mymemory,cloudflare,googleweb"
ENGINE_CHOICES = {"auto", *PROVIDERS}


def order() -> List[str]:
    names = [n.strip() for n in os.getenv("TRANSLATE_ORDER", DEFAULT_ORDER).split(",") if n.strip() in PROVIDERS]
    return names + [n for n in PROVIDERS if n not in names]


# ---------------------------------------------------------------------------
# Health tracking + cool-down ("circuit breaker"), per server instance
# ---------------------------------------------------------------------------
_health: Dict[str, dict] = {n: {"ok": 0, "fail": 0, "consec": 0, "cool_until": 0.0, "last_error": None,
                                "last_error_at": None, "last_ok_at": None, "rate_strikes": 0} for n in PROVIDERS}
_hlock = threading.Lock()


def _cooldown_for(e: ProviderError, h: dict) -> float:
    if e.kind == "quota":
        return e.retry_after or 3600
    if e.kind == "rate":
        h["rate_strikes"] += 1
        base = e.retry_after or 60
        return min(base * (2 ** (h["rate_strikes"] - 1)), 1800)
    if e.kind in ("auth", "config"):
        return 1800
    return 60 if h["consec"] >= 3 else 0


def _mark_ok(name: str):
    with _hlock:
        h = _health[name]
        h["ok"] += 1
        h["consec"] = 0
        h["rate_strikes"] = 0
        h["cool_until"] = 0.0
        h["last_ok_at"] = time.time()


def _mark_fail(name: str, e: ProviderError):
    with _hlock:
        h = _health[name]
        h["fail"] += 1
        h["consec"] += 1
        h["last_error"] = str(e)
        h["last_kind"] = e.kind
        h["last_error_at"] = time.time()
        cd = _cooldown_for(e, h)
        if cd:
            h["cool_until"] = max(h["cool_until"], time.time() + cd)
    log.warning("translate provider %s failed (%s): %s", name, e.kind, e)


def cooling(name: str) -> float:
    return max(0.0, _health[name]["cool_until"] - time.time())


def status() -> List[dict]:
    out = []
    for n in order():
        p, h = PROVIDERS[n], _health[n]
        out.append({"name": n, "label": p.label, "note": p.note, "configured": p.configured(),
                    "needs": list(p.needs), "cooldown_seconds": int(cooling(n)), "ok": h["ok"], "fail": h["fail"],
                    "last_error": h["last_error"], "last_error_at": h["last_error_at"], "last_ok_at": h["last_ok_at"]})
    return out


def reset_cooldowns():
    with _hlock:
        for h in _health.values():
            h["cool_until"] = 0.0
            h["rate_strikes"] = 0
            h["consec"] = 0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def translate(text: str, engine: str = "auto") -> Tuple[str, str, List[str]]:
    """Translate Thai text to Burmese. Returns (burmese, provider_name, notes).
    `engine` is the user's preferred provider; if it fails we fall back automatically."""
    text = text.strip()
    if not text:
        raise ProviderError("ไม่มีข้อความ", "error")
    chain = order()
    if engine in PROVIDERS:
        chain = [engine] + [n for n in chain if n != engine]
    chain = [n for n in chain if PROVIDERS[n].configured()]

    deadline = time.time() + TOTAL_BUDGET
    attempts: List[Tuple[str, str]] = []
    notes: List[str] = []
    skipped = []
    for n in chain:
        if cooling(n) > 0:
            skipped.append(n)
            continue
        if time.time() > deadline:
            break
        try:
            out = PROVIDERS[n].translate(text)
            _mark_ok(n)
            return out, n, notes
        except ProviderError as e:
            _mark_fail(n, e)
            attempts.append((n, KIND_TH.get(e.kind, "") if e.kind != "error" else str(e)[:80]))
            notes.append(f"{PROVIDERS[n].label}: {KIND_TH.get(e.kind, e.kind)}")
        except Exception as e:  # never let one provider's bug stop the chain
            pe = ProviderError(str(e)[:150], "error")
            _mark_fail(n, pe)
            attempts.append((n, str(e)[:80]))
    # Last resort: everything is cooling down -> try those once anyway (except bad keys/config)
    for n in skipped:
        if _health[n].get("last_kind") in ("auth", "config"):
            continue  # a wrong key / setting will not fix itself by retrying
        if time.time() > deadline:
            break
        try:
            out = PROVIDERS[n].translate(text)
            _mark_ok(n)
            return out, n, notes
        except ProviderError as e:
            _mark_fail(n, e)
            attempts.append((n, KIND_TH.get(e.kind, "")))
        except Exception as e:
            _mark_fail(n, ProviderError(str(e)[:150], "error"))
            attempts.append((n, str(e)[:80]))
    raise AllProvidersFailed(attempts)


def test_all(sample: str = "สวัสดีครับ กรุณาสวมหมวกนิรภัย") -> List[dict]:
    """Admin button: try every configured provider once (ignores cool-down)."""
    res = []
    for n in order():
        p = PROVIDERS[n]
        if not p.configured():
            res.append({"name": n, "label": p.label, "ok": False, "result": "ยังไม่ได้ตั้งค่า (" + ", ".join(p.needs) + ")"})
            continue
        t0 = time.time()
        try:
            out = p.translate(sample)
            _mark_ok(n)
            res.append({"name": n, "label": p.label, "ok": True, "result": out, "ms": int((time.time() - t0) * 1000)})
        except Exception as e:
            pe = e if isinstance(e, ProviderError) else ProviderError(str(e)[:150])
            _mark_fail(n, pe)
            res.append({"name": n, "label": p.label, "ok": False, "result": str(pe), "ms": int((time.time() - t0) * 1000)})
    return res
