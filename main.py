from fastapi import FastAPI, Form, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, FileResponse
import edge_tts
from deep_translator import GoogleTranslator, MyMemoryTranslator
import os
import signal
import subprocess
import sqlite3
import hashlib
import jwt
import datetime

SECRET_KEY = "super-secret-key-for-jwt"
DB_FILE = "app.db"

app = FastAPI()

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE,
                    password_hash TEXT,
                    role TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT)''')
    
    c.execute("SELECT * FROM users WHERE username='admin'")
    if not c.fetchone():
        pwd_hash = hashlib.sha256("123456".encode()).hexdigest()
        c.execute("INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)", 
                  ("admin", pwd_hash, "admin"))
    
    defaults = {
        "default_engine": "auto",
        "default_rate": "+15%",
        "default_pitch": "+0Hz",
        "default_voice": "my-MM-NilarNeural",
        "default_format": "mp3"
    }
    for k, v in defaults.items():
        c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))
        
    conn.commit()
    conn.close()

init_db()

def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def convert_audio(input_file: str, output_file: str):
    import imageio_ffmpeg
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    command = [ffmpeg_exe, '-y', '-i', input_file, output_file]
    subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def verify_token(request: Request):
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid token")
    token = auth_header.split(" ")[1]
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")

@app.get("/", response_class=HTMLResponse)
async def get_home():
    with open("index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read(), status_code=200)

@app.post("/login")
async def login(username: str = Form(...), password: str = Form(...)):
    pwd_hash = hashlib.sha256(password.encode()).hexdigest()
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE username=? AND password_hash=?", (username, pwd_hash)).fetchone()
    conn.close()
    
    if user:
        payload = {
            "sub": user["username"],
            "role": user["role"],
            "exp": datetime.datetime.utcnow() + datetime.timedelta(hours=24)
        }
        token = jwt.encode(payload, SECRET_KEY, algorithm="HS256")
        return {"access_token": token, "role": user["role"]}
    else:
        raise HTTPException(status_code=401, detail="Invalid credentials")

# --- USER MANAGEMENT ENDPOINTS ---
@app.get("/api/users")
async def get_users(user = Depends(verify_token)):
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    conn = get_db()
    rows = conn.execute("SELECT id, username, role FROM users").fetchall()
    conn.close()
    return [{"id": r["id"], "username": r["username"], "role": r["role"]} for r in rows]

@app.post("/api/users")
async def create_user(
    username: str = Form(...),
    password: str = Form(...),
    role: str = Form("user"),
    user = Depends(verify_token)
):
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    conn = get_db()
    existing = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
    if existing:
        conn.close()
        raise HTTPException(status_code=400, detail="มีชื่อผู้ใช้งานนี้อยู่ในระบบแล้ว")
    
    pwd_hash = hashlib.sha256(password.encode()).hexdigest()
    conn.execute("INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)", (username, pwd_hash, role))
    conn.commit()
    conn.close()
    return {"status": "success"}

@app.delete("/api/users/{user_id}")
async def delete_user(user_id: int, user = Depends(verify_token)):
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    conn = get_db()
    target = conn.execute("SELECT username FROM users WHERE id=?", (user_id,)).fetchone()
    if target and target["username"] == "admin":
        conn.close()
        raise HTTPException(status_code=400, detail="ไม่สามารถลบบัญชี Admin เริ่มต้นได้")
    
    conn.execute("DELETE FROM users WHERE id=?", (user_id,))
    conn.commit()
    conn.close()
    return {"status": "success"}


# --- SETTINGS ENDPOINTS ---
@app.get("/api/settings")
async def get_settings(user = Depends(verify_token)):
    conn = get_db()
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    conn.close()
    return {row["key"]: row["value"] for row in rows}

@app.post("/api/settings")
async def update_settings(
    request: Request,
    default_engine: str = Form(...),
    default_rate: str = Form(...),
    default_pitch: str = Form(...),
    default_voice: str = Form(...),
    default_format: str = Form(...),
    user = Depends(verify_token)
):
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    
    conn = get_db()
    settings = {
        "default_engine": default_engine,
        "default_rate": default_rate,
        "default_pitch": default_pitch,
        "default_voice": default_voice,
        "default_format": default_format
    }
    for k, v in settings.items():
        conn.execute("UPDATE settings SET value=? WHERE key=?", (v, k))
    conn.commit()
    conn.close()
    return {"status": "success"}

# --- TRANSLATION ENDPOINT ---
@app.post("/process")
async def process_text(
    text: str = Form(...), 
    rate: str = Form(...), 
    pitch: str = Form(...),
    voice: str = Form(...),
    audio_format: str = Form(...),
    engine: str = Form(...),
    user = Depends(verify_token)
):
    try:
        burmese_text = ""
        used_engine = engine

        if engine == "google":
            burmese_text = GoogleTranslator(source='th', target='my').translate(text)
        elif engine == "mymemory":
            burmese_text = MyMemoryTranslator(source='th-TH', target='my-MM').translate(text)
        else:
            try:
                burmese_text = GoogleTranslator(source='th', target='my').translate(text)
                used_engine = "google (auto)"
            except Exception as e:
                burmese_text = MyMemoryTranslator(source='th-TH', target='my-MM').translate(text)
                used_engine = "mymemory (fallback)"

        if not burmese_text:
            return {"error": "ไม่สามารถแปลภาษาได้ในขณะนี้ กรุณาลองใหม่"}

        base_file = "output.mp3"
        communicate = edge_tts.Communicate(burmese_text, voice, rate=rate, pitch=pitch)
        await communicate.save(base_file)

        final_file = f"output.{audio_format}"
        if audio_format != "mp3":
            convert_audio(base_file, final_file)

        return {
            "burmese_text": burmese_text,
            "engine_used": used_engine,
            "play_url": f"/audio?fmt={audio_format}",
            "download_url": f"/download?fmt={audio_format}"
        }
    except Exception as e:
        return {"error": f"API Error: {str(e)}"}

@app.get("/audio")
async def get_audio(fmt: str = "mp3"):
    file_path = f"output.{fmt}"
    if os.path.exists(file_path):
        media_type = f"audio/{fmt}" if fmt != "mp3" else "audio/mpeg"
        return FileResponse(file_path, media_type=media_type)
    return {"error": "File not found"}

@app.get("/download")
async def download_audio(fmt: str = "mp3"):
    file_path = f"output.{fmt}"
    if os.path.exists(file_path):
        media_type = f"audio/{fmt}" if fmt != "mp3" else "audio/mpeg"
        return FileResponse(file_path, media_type=media_type, headers={"Content-Disposition": f"attachment; filename=burmese_audio.{fmt}"})
    return {"error": "File not found"}

@app.post("/shutdown")
def shutdown_server():
    os.kill(os.getpid(), signal.SIGINT)
    return {"status": "success"}
