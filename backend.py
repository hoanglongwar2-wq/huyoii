import os, io, re, ast, json, time, uuid, base64, binascii, logging, shutil, tempfile, sqlite3, zipfile, secrets, posixpath, hmac, hashlib
import urllib.request, urllib.parse
from datetime import timedelta
from functools import wraps
from flask import Flask, request, jsonify, send_from_directory, send_file, after_this_request, session, g
from flask_cors import CORS
from dotenv import load_dotenv
from google import genai
from google.genai import types
from pypdf import PdfReader
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import generate_password_hash, check_password_hash

load_dotenv()
try:
    import config as _cfg          # file config.py cạnh backend.py (xem hướng dẫn trong file đó)
except Exception:
    _cfg = None

def cfg(name, default=""):
    """Ưu tiên biến môi trường (Render > Environment), sau đó đến config.py."""
    v = os.getenv(name)
    if v is not None and v.strip() != "":
        return v
    v = getattr(_cfg, name, default) if _cfg else default
    return default if v is None else v

def split_keys(x):
    if isinstance(x, (list, tuple, set)):
        items = [str(i) for i in x]
    else:
        items = re.split(r"[,;\s]+", str(x or ""))
    return [i.strip() for i in items if 20 <= len(i.strip()) <= 200]
logging.basicConfig(level=logging.INFO)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MB = 1024 * 1024

# ------------------------------------------------------------------ cấu hình
DB_PATH = os.getenv("DB_PATH", "").strip() or os.path.join(BASE_DIR, "data", "huy_ai.db")
ADMIN_EMAIL = str(cfg("ADMIN_EMAIL")).strip().lower()
ADMIN_PASSWORD = str(cfg("ADMIN_PASSWORD"))
GOOGLE_CLIENT_ID = str(cfg("GOOGLE_CLIENT_ID")).strip()
FACEBOOK_APP_ID = str(cfg("FACEBOOK_APP_ID")).strip()
FACEBOOK_APP_SECRET = str(cfg("FACEBOOK_APP_SECRET")).strip()
ALLOW_REGISTER = os.getenv("ALLOW_REGISTER", "1").strip().lower() not in ("0", "false", "no", "off")
MAX_CODE_BYTES = 50 * MB                                   # file/zip code tối đa 50 MB
USER_QUOTA = int(os.getenv("USER_QUOTA_MB", "300")) * MB   # dung lượng file lưu cho mỗi tài khoản
CORS_LIST = [o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()]
DEFAULT_TITLE = "Cuộc trò chuyện mới"

SECRET_KEY = str(cfg("SECRET_KEY")).strip()
if not SECRET_KEY:
    SECRET_KEY = secrets.token_hex(32)
    logging.warning("Chưa đặt SECRET_KEY: phiên đăng nhập sẽ mất mỗi lần máy chủ khởi động lại.")

app = Flask(__name__, static_folder="static")
# Render đứng sau proxy: lấy IP thật của người dùng để rate-limit không bị gộp chung
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.config["MAX_CONTENT_LENGTH"] = 40 * MB
app.config.update(
    SECRET_KEY=SECRET_KEY,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="None" if CORS_LIST else "Lax",
    SESSION_COOKIE_SECURE=bool(CORS_LIST) or bool(os.getenv("RENDER")) or os.getenv("COOKIE_SECURE") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)
limiter = Limiter(get_remote_address, app=app, default_limits=["300 per minute"], storage_uri="memory://")
if CORS_LIST:   # chỉ bật CORS khi chủ động khai báo origin (frontend tách riêng)
    CORS(app, resources={r"/api/*": {"origins": CORS_LIST}}, supports_credentials=True)

# Danh sách key phía máy chủ: GEMINI_API_KEY / GEMINI_API_KEYS (biến môi trường) + config.py. Key lỗi thì tự chuyển sang key kế tiếp.
SERVER_KEYS = []
for _src in (os.getenv("GEMINI_API_KEY", ""), os.getenv("GEMINI_API_KEYS", ""),
             getattr(_cfg, "GEMINI_API_KEY", "") if _cfg else "", getattr(_cfg, "GEMINI_API_KEYS", "") if _cfg else ""):
    for _k in split_keys(_src):
        if _k not in SERVER_KEYS: SERVER_KEYS.append(_k)
CHAT_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip()
IMAGE_MODEL = os.getenv("GEMINI_IMAGE_MODEL", "gemini-3.1-flash-image").strip()
client = bool(SERVER_KEYS)
_client_cache, _bad_until = {}, {}

def client_for(key):
    c = _client_cache.get(key)
    if c is None:
        if len(_client_cache) > 60: _client_cache.clear()
        c = _client_cache[key] = genai.Client(api_key=key)
    return c

def db_keys():
    """Key do admin lưu trong trang Cài đặt (lưu trong cơ sở dữ liệu)."""
    try:
        r = q1("SELECT v FROM settings WHERE k='gemini_keys'")
        return split_keys(r["v"]) if r else []
    except Exception:
        return []

def mask_key(k): return k[:4] + "…" + k[-4:]

def key_chain():
    """Thứ tự thử: key admin nhập trong ⚙ (trình duyệt) -> key admin lưu trên máy chủ -> key trên máy chủ (env/config.py) -> key dự phòng nhúng trong HTML.
    Key vừa bị lỗi hạn mức/không hợp lệ được đẩy xuống cuối trong 60 giây."""
    chain = []
    def add(keys):
        for k in keys:
            if k not in chain: chain.append(k)
    u = current_user()
    if u and u["is_admin"]:            # chỉ admin được dùng key nhập ở ⚙ trên trình duyệt
        add(split_keys(request.headers.get("X-Api-Key", ""))[:1])
    add(db_keys())
    add(SERVER_KEYS)
    add(split_keys(request.headers.get("X-Api-Key-Backup", ""))[:5])
    now = time.time()
    return [k for k in chain if _bad_until.get(k, 0) <= now] + [k for k in chain if _bad_until.get(k, 0) > now]

def is_key_error(exc):
    m = str(exc).lower()
    return any(x in m for x in ("quota", "429", "resource_exhausted", "rate limit", "api key not valid", "api_key_invalid",
                                 "permission_denied", "unauthorized", "401", "403", "billing", "expired", "api key"))

SYSTEM = os.getenv("SYSTEM_INSTRUCTION",
    "Bạn là trợ lý AI của Nguyễn Anh Huy, nói tiếng Việt thân thiện, chính xác, trả lời rõ ràng có cấu trúc. "
    "Nếu thiếu dữ kiện hoặc không chắc chắn, hãy nói rõ. Không bịa nguồn.")

CODE_SYSTEM = """Bạn là kỹ sư phần mềm chuyên nghiệp, viết được MỌI loại code và định dạng văn bản: Python, JavaScript/TypeScript, HTML/CSS, Java, Kotlin, C, C++, C#, Go, Rust, PHP, Ruby, Swift, Dart, SQL, Bash/PowerShell/Batch, Arduino, Dockerfile, YAML, JSON, XML, Markdown, cấu hình dự án...
Luôn trả lời ĐÚNG định dạng sau (backend sẽ tự đóng gói thành file tải về):

=== NOTE ===
(1-6 dòng tiếng Việt: đã làm gì, cách chạy, nếu là sửa lỗi thì nêu lỗi và nguyên nhân)
=== FILE: duong/dan/ten_file.ext ===
(nội dung file, code thô, KHÔNG bọc trong ```)
=== END FILE ===

Quy tắc:
- Mỗi file một khối FILE ... END FILE. Dự án nhiều file thì viết nhiều khối với đường dẫn tương đối (vd src/main.py, index.html); backend sẽ nén thành .zip. Chỉ một file thì xuất một khối.
- Code phải hoàn chỉnh, chạy được ngay, không để chỗ trống kiểu "..." hay "phần còn lại giữ nguyên". Giải thích cần thiết đặt trong comment.
- Nếu trong cuộc trò chuyện đã có file đã tạo trước đó và người dùng báo lỗi (kèm thông báo lỗi/ảnh chụp) hoặc yêu cầu thay đổi: sửa trên đúng phiên bản mới nhất, giữ nguyên đường dẫn, và xuất lại ĐẦY ĐỦ từng file đã thay đổi (file không đổi có thể bỏ qua nếu dự án nhiều file; nếu không chắc thì xuất lại cả dự án).
- Nếu người dùng chỉ hỏi/giải thích mà không cần file, chỉ viết khối NOTE, không viết FILE.
- Không viết gì ngoài các khối trên."""

# ------------------------------------------------------------------ cơ sở dữ liệu (SQLite)
SCHEMA = [
    """CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE NOT NULL, name TEXT NOT NULL, pw_hash TEXT NOT NULL,
        is_admin INTEGER NOT NULL DEFAULT 0, is_active INTEGER NOT NULL DEFAULT 1, token_ver INTEGER NOT NULL DEFAULT 0,
        failed INTEGER NOT NULL DEFAULT 0, locked_until REAL NOT NULL DEFAULT 0, created REAL NOT NULL, last_login REAL)""",
    """CREATE TABLE IF NOT EXISTS conversations(
        id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        title TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS messages(
        id INTEGER PRIMARY KEY AUTOINCREMENT, conv_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        role TEXT NOT NULL, body TEXT NOT NULL, created REAL NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS files(
        id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        conv_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
        name TEXT NOT NULL, mime TEXT NOT NULL, size INTEGER NOT NULL, kind TEXT NOT NULL, data BLOB NOT NULL, created REAL NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS idx_conv_user ON conversations(user_id, updated)",
    "CREATE INDEX IF NOT EXISTS idx_msg_conv ON messages(conv_id, id)",
    "CREATE INDEX IF NOT EXISTS idx_files_msg ON files(message_id)",
    "CREATE INDEX IF NOT EXISTS idx_files_user ON files(user_id)",
]

def _connect():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c

def db():
    if "db" not in g: g.db = _connect()
    return g.db

@app.teardown_appcontext
def _close_db(_exc):
    c = g.pop("db", None)
    if c is not None: c.close()

def q(sql, args=()):
    return [dict(r) for r in db().execute(sql, args).fetchall()]

def q1(sql, args=()):
    r = db().execute(sql, args).fetchone()
    return dict(r) if r else None

def ex(sql, args=(), commit=True):
    cur = db().execute(sql, args)
    if commit: db().commit()
    return cur

def init_db():
    global DB_PATH
    try:
        os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    except OSError:
        DB_PATH = os.path.join(tempfile.gettempdir(), "huy_ai.db")
        logging.warning("Không ghi được vào thư mục dữ liệu, tạm dùng %s", DB_PATH)
    c = _connect()
    try:
        c.execute("PRAGMA journal_mode=WAL")
        for s in SCHEMA: c.execute(s)
        c.execute("CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT NOT NULL)")
        try: c.execute("ALTER TABLE users ADD COLUMN social INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError: pass
        c.commit()
    finally:
        c.close()
    with app.app_context():
        seed_admin()

def seed_admin():
    """Tài khoản admin: ADMIN_EMAIL / ADMIN_PASSWORD (config.py hoặc biến môi trường); đổi giá trị là đổi mật khẩu admin.
    Nếu chưa khai báo thì tự tạo admin@huy.local với mật khẩu cố định suy ra từ SECRET_KEY và in ra log để bạn đăng nhập lần đầu."""
    global ADMIN_EMAIL, ADMIN_PASSWORD
    if not ADMIN_EMAIL or len(ADMIN_PASSWORD) < 8:
        if ADMIN_PASSWORD: logging.warning("ADMIN_PASSWORD phải từ 8 ký tự -> dùng tài khoản admin tự tạo.")
        ADMIN_EMAIL = ADMIN_EMAIL or "admin@huy.local"
        ADMIN_PASSWORD = hmac.new(SECRET_KEY.encode(), b"admin-password", hashlib.sha256).hexdigest()[:14]
        logging.warning("=== ADMIN TỰ TẠO === email: %s | mật khẩu: %s  (hãy đặt ADMIN_EMAIL/ADMIN_PASSWORD của bạn trong config.py hoặc Render > Environment)", ADMIN_EMAIL, ADMIN_PASSWORD)
    now = time.time()
    u = q1("SELECT * FROM users WHERE email=?", (ADMIN_EMAIL,))
    if u is None:
        ex("INSERT INTO users(email,name,pw_hash,is_admin,created) VALUES(?,?,?,1,?)",
           (ADMIN_EMAIL, "Admin", generate_password_hash(ADMIN_PASSWORD), now))
        logging.info("Đã tạo tài khoản admin %s", ADMIN_EMAIL)
    else:
        if not check_password_hash(u["pw_hash"], ADMIN_PASSWORD):
            ex("UPDATE users SET pw_hash=?, token_ver=token_ver+1 WHERE id=?", (generate_password_hash(ADMIN_PASSWORD), u["id"]))
        ex("UPDATE users SET is_admin=1, is_active=1, failed=0, locked_until=0, social=0 WHERE id=?", (u["id"],))
    ex("UPDATE users SET is_admin=0 WHERE email<>? AND is_admin=1", (ADMIN_EMAIL,))

# ------------------------------------------------------------------ tài khoản / phiên đăng nhập
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")
DUMMY_HASH = generate_password_hash("dummy-password-for-timing")
MAX_FAILED, LOCK_SECONDS = 8, 15 * 60

def public_user(u):
    return {"id": u["id"], "email": u["email"], "name": u["name"], "is_admin": bool(u["is_admin"])}

def current_user():
    if "user" in g: return g.user
    u = None
    uid = session.get("uid")
    if uid:
        row = q1("SELECT * FROM users WHERE id=?", (uid,))
        if row and row["is_active"] and row["token_ver"] == session.get("tv"): u = row
    g.user = u
    return u

def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if current_user() is None:
            return jsonify({"error": "Bạn chưa đăng nhập hoặc phiên đã hết hạn."}), 401
        return fn(*a, **kw)
    return wrapper

def admin_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        u = current_user()
        if u is None: return jsonify({"error": "Bạn chưa đăng nhập hoặc phiên đã hết hạn."}), 401
        if not u["is_admin"]: return jsonify({"error": "Chỉ admin mới được dùng chức năng này."}), 403
        return fn(*a, **kw)
    return wrapper

def start_session(u):
    session.clear()
    session.permanent = True
    session["uid"], session["tv"] = u["id"], u["token_ver"]

def json_body():
    return request.get_json(silent=True) or {}

@app.post("/api/auth/register")
@limiter.limit("10 per hour")
def register():
    if not ALLOW_REGISTER:
        return jsonify({"error": "Máy chủ đang tắt đăng ký tài khoản mới. Liên hệ admin."}), 403
    d = json_body()
    name = str(d.get("name", "")).strip()
    email = str(d.get("email", "")).strip().lower()
    pw = str(d.get("password", ""))
    if not (2 <= len(name) <= 40): return jsonify({"error": "Tên hiển thị phải từ 2 đến 40 ký tự."}), 400
    if len(email) > 120 or not EMAIL_RE.match(email): return jsonify({"error": "Email không hợp lệ."}), 400
    if not (8 <= len(pw) <= 128): return jsonify({"error": "Mật khẩu phải từ 8 đến 128 ký tự."}), 400
    if email == ADMIN_EMAIL or q1("SELECT 1 AS x FROM users WHERE email=?", (email,)):
        return jsonify({"error": "Email này đã được đăng ký."}), 409
    now = time.time()
    uid = ex("INSERT INTO users(email,name,pw_hash,created,last_login) VALUES(?,?,?,?,?)",
             (email, name, generate_password_hash(pw), now, now)).lastrowid
    u = q1("SELECT * FROM users WHERE id=?", (uid,))
    start_session(u)
    return jsonify({"user": public_user(u)})

@app.post("/api/auth/login")
@limiter.limit("10 per minute")
def login():
    d = json_body()
    email = str(d.get("email", "")).strip().lower()
    pw = str(d.get("password", ""))
    u = q1("SELECT * FROM users WHERE email=?", (email,)) if email else None
    bad = jsonify({"error": "Email hoặc mật khẩu không đúng."}), 401
    if u is None:
        check_password_hash(DUMMY_HASH, pw)   # giữ thời gian phản hồi gần như nhau
        return bad
    now = time.time()
    if u["locked_until"] > now:
        mins = int((u["locked_until"] - now) // 60) + 1
        return jsonify({"error": f"Nhập sai quá nhiều lần. Thử lại sau {mins} phút."}), 429
    if not check_password_hash(u["pw_hash"], pw):
        failed = u["failed"] + 1
        if failed >= MAX_FAILED: ex("UPDATE users SET failed=0, locked_until=? WHERE id=?", (now + LOCK_SECONDS, u["id"]))
        else: ex("UPDATE users SET failed=? WHERE id=?", (failed, u["id"]))
        return bad
    if not u["is_active"]:
        return jsonify({"error": "Tài khoản này đã bị admin khóa."}), 403
    ex("UPDATE users SET failed=0, locked_until=0, last_login=? WHERE id=?", (now, u["id"]))
    start_session(u)
    return jsonify({"user": public_user(u)})


# ------------------------------------------------------------------ đăng nhập bằng Google / Facebook
def http_json(url, timeout=8):
    req = urllib.request.Request(url, headers={"User-Agent": "huy-ai"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))

@app.get("/api/auth/config")
@limiter.exempt
def auth_config():
    return jsonify({"google": GOOGLE_CLIENT_ID, "facebook": FACEBOOK_APP_ID, "register": ALLOW_REGISTER})

def social_login(email, name, allow_admin=False):
    """Đăng nhập/tạo tài khoản từ nhà cung cấp đã xác minh email. Trả về response."""
    email = (email or "").strip().lower()
    name = (name or email.split("@")[0] or "Người dùng").strip()[:40]
    if len(name) < 2: name = (name + " ·")[:40]
    if not EMAIL_RE.match(email) or len(email) > 120:
        return jsonify({"error": "Không lấy được email hợp lệ từ tài khoản này."}), 400
    now = time.time()
    u = q1("SELECT * FROM users WHERE email=?", (email,))
    if u is None:
        if not ALLOW_REGISTER:
            return jsonify({"error": "Máy chủ đang tắt đăng ký tài khoản mới. Liên hệ admin."}), 403
        uid = ex("INSERT INTO users(email,name,pw_hash,social,created,last_login) VALUES(?,?,?,1,?,?)",
                 (email, name, generate_password_hash(secrets.token_urlsafe(32)), now, now)).lastrowid
    else:
        if u["is_admin"] and not allow_admin:
            return jsonify({"error": "Tài khoản admin chỉ đăng nhập bằng email + mật khẩu hoặc Google."}), 403
        if not u["is_active"]:
            return jsonify({"error": "Tài khoản này đã bị admin khóa."}), 403
        uid = u["id"]
        if not u["social"] and not u["is_admin"]:
            # chống chiếm tài khoản: email chưa từng được xác minh khi đăng ký bằng mật khẩu -> vô hiệu mật khẩu cũ
            ex("UPDATE users SET pw_hash=?, token_ver=token_ver+1, social=1 WHERE id=?", (generate_password_hash(secrets.token_urlsafe(32)), uid))
        ex("UPDATE users SET last_login=? WHERE id=?", (now, uid))
    u = q1("SELECT * FROM users WHERE id=?", (uid,))
    start_session(u)
    return jsonify({"user": public_user(u)})

@app.post("/api/auth/google")
@limiter.limit("20 per minute")
def auth_google():
    if not GOOGLE_CLIENT_ID: return jsonify({"error": "Máy chủ chưa bật đăng nhập Google."}), 503
    tok = str(json_body().get("credential", ""))
    if not tok or len(tok) > 4000: return jsonify({"error": "Thiếu thông tin đăng nhập Google."}), 400
    try:
        d = http_json("https://oauth2.googleapis.com/tokeninfo?" + urllib.parse.urlencode({"id_token": tok}))
    except Exception:
        return jsonify({"error": "Google từ chối phiên đăng nhập này. Thử lại."}), 401
    if d.get("aud") != GOOGLE_CLIENT_ID or d.get("iss") not in ("accounts.google.com", "https://accounts.google.com") \
       or str(d.get("email_verified")).lower() != "true" or int(d.get("exp", 0) or 0) < time.time():
        return jsonify({"error": "Phiên đăng nhập Google không hợp lệ."}), 401
    return social_login(d.get("email"), d.get("name"), allow_admin=True)

@app.post("/api/auth/facebook")
@limiter.limit("20 per minute")
def auth_facebook():
    if not (FACEBOOK_APP_ID and FACEBOOK_APP_SECRET): return jsonify({"error": "Máy chủ chưa bật đăng nhập Facebook."}), 503
    tok = str(json_body().get("accessToken", ""))
    if not tok or len(tok) > 1000: return jsonify({"error": "Thiếu thông tin đăng nhập Facebook."}), 400
    try:
        dbg = http_json("https://graph.facebook.com/debug_token?" + urllib.parse.urlencode(
            {"input_token": tok, "access_token": FACEBOOK_APP_ID + "|" + FACEBOOK_APP_SECRET})).get("data", {})
        if not dbg.get("is_valid") or str(dbg.get("app_id")) != FACEBOOK_APP_ID:
            return jsonify({"error": "Phiên đăng nhập Facebook không hợp lệ."}), 401
        prof = http_json("https://graph.facebook.com/me?" + urllib.parse.urlencode({"fields": "id,name,email", "access_token": tok}))
    except Exception:
        return jsonify({"error": "Facebook từ chối phiên đăng nhập này. Thử lại."}), 401
    if not prof.get("id"): return jsonify({"error": "Không đọc được hồ sơ Facebook."}), 401
    # Facebook không phải lúc nào cũng trả email: khi thiếu thì dùng định danh riêng của Facebook
    email = prof.get("email") or f"fb_{prof['id']}@facebook.local"
    return social_login(email, prof.get("name"))

@app.post("/api/auth/logout")
def logout():
    session.clear()
    return jsonify({"ok": True})

@app.get("/api/auth/me")
def me():
    u = current_user()
    if u is None: return jsonify({"error": "Chưa đăng nhập."}), 401
    return jsonify({"user": public_user(u)})

@app.post("/api/auth/password")
@limiter.limit("10 per hour")
@login_required
def change_password():
    d, u = json_body(), current_user()
    old, new = str(d.get("current", "")), str(d.get("new", ""))
    if not check_password_hash(u["pw_hash"], old): return jsonify({"error": "Mật khẩu hiện tại không đúng."}), 400
    if not (8 <= len(new) <= 128): return jsonify({"error": "Mật khẩu mới phải từ 8 đến 128 ký tự."}), 400
    if u["is_admin"] and ADMIN_PASSWORD:
        return jsonify({"error": "Mật khẩu admin được đặt bằng biến môi trường ADMIN_PASSWORD trên máy chủ; hãy đổi ở đó."}), 400
    ex("UPDATE users SET pw_hash=?, token_ver=token_ver+1 WHERE id=?", (generate_password_hash(new), u["id"]))
    start_session(q1("SELECT * FROM users WHERE id=?", (u["id"],)))
    return jsonify({"ok": True})

# ------------------------------------------------------------------ hội thoại & tệp
def get_conv(cid):
    u = current_user()
    return q1("SELECT * FROM conversations WHERE id=? AND user_id=?", (str(cid or ""), u["id"]))

def title_from(text):
    t = " ".join(str(text).split())[:34].strip()
    return t or DEFAULT_TITLE

def user_storage(uid):
    return int(q1("SELECT COALESCE(SUM(size),0) AS s FROM files WHERE user_id=?", (uid,))["s"])

def add_message(conv, role, body, files=()):
    """Lưu tin nhắn (và tệp đính kèm) vào hội thoại. Trả về (message_id, [thông tin tệp])."""
    now = time.time()
    c = db()
    mid = c.execute("INSERT INTO messages(conv_id,role,body,created) VALUES(?,?,?,?)", (conv["id"], role, body, now)).lastrowid
    out = []
    for f in files:
        fid = uuid.uuid4().hex
        c.execute("INSERT INTO files(id,user_id,conv_id,message_id,name,mime,size,kind,data,created) VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (fid, conv["user_id"], conv["id"], mid, f["name"], f["mime"], len(f["data"]), f["kind"], f["data"], now))
        out.append({"id": fid, "name": f["name"], "mime": f["mime"], "size": len(f["data"]), "kind": f["kind"]})
    title = conv["title"]
    if role == "user" and title == DEFAULT_TITLE:
        title = title_from(body)
    c.execute("UPDATE conversations SET updated=?, title=? WHERE id=?", (now, title, conv["id"]))
    c.commit()
    conv["title"] = title
    return mid, out

@app.get("/api/conversations")
@login_required
def list_conversations():
    rows = q("SELECT id,title,updated FROM conversations WHERE user_id=? ORDER BY updated DESC LIMIT 500", (current_user()["id"],))
    return jsonify({"conversations": rows})

@app.post("/api/conversations")
@limiter.limit("60 per hour")
@login_required
def create_conversation():
    u, now, cid = current_user(), time.time(), uuid.uuid4().hex
    ex("INSERT INTO conversations(id,user_id,title,created,updated) VALUES(?,?,?,?,?)", (cid, u["id"], DEFAULT_TITLE, now, now))
    return jsonify({"conversation": {"id": cid, "title": DEFAULT_TITLE, "updated": now}})

@app.get("/api/conversations/<cid>")
@login_required
def read_conversation(cid):
    conv = get_conv(cid)
    if not conv: return jsonify({"error": "Không tìm thấy cuộc trò chuyện."}), 404
    msgs = q("SELECT id,role,body AS text,created FROM messages WHERE conv_id=? ORDER BY id", (cid,))
    by_msg = {}
    for f in q("SELECT id,message_id,name,mime,size,kind FROM files WHERE conv_id=? ORDER BY created", (cid,)):
        by_msg.setdefault(f.pop("message_id"), []).append(f)
    for m in msgs: m["files"] = by_msg.get(m["id"], [])
    return jsonify({"conversation": {"id": conv["id"], "title": conv["title"], "updated": conv["updated"]}, "messages": msgs})

@app.patch("/api/conversations/<cid>")
@login_required
def rename_conversation(cid):
    conv = get_conv(cid)
    if not conv: return jsonify({"error": "Không tìm thấy cuộc trò chuyện."}), 404
    title = " ".join(str(json_body().get("title", "")).split())[:80]
    if not title: return jsonify({"error": "Tiêu đề không được để trống."}), 400
    ex("UPDATE conversations SET title=? WHERE id=?", (title, cid))
    return jsonify({"ok": True, "title": title})

@app.delete("/api/conversations/<cid>")
@login_required
def delete_conversation(cid):
    if not get_conv(cid): return jsonify({"error": "Không tìm thấy cuộc trò chuyện."}), 404
    ex("DELETE FROM conversations WHERE id=?", (cid,))
    return jsonify({"ok": True})

@app.post("/api/conversations/<cid>/messages")
@limiter.limit("60 per minute")
@login_required
def append_message(cid):
    """Ghi thêm một tin nhắn ngắn (dùng cho ghi chú tải video/MP3)."""
    conv = get_conv(cid)
    if not conv: return jsonify({"error": "Không tìm thấy cuộc trò chuyện."}), 404
    d = json_body()
    role = "user" if d.get("role") == "user" else "ai"
    text = str(d.get("text", "")).strip()[:2000]
    if not text: return jsonify({"error": "Tin nhắn trống."}), 400
    add_message(conv, role, text)
    return jsonify({"ok": True, "title": conv["title"]})

@app.post("/api/import")
@limiter.limit("5 per hour")
@login_required
def import_local():
    """Nhập lịch sử cũ (chỉ phần chữ) đang nằm trong localStorage của trình duyệt vào tài khoản."""
    u, items = current_user(), json_body().get("conversations")
    if not isinstance(items, list): return jsonify({"error": "Dữ liệu không hợp lệ."}), 400
    n, t = 0, time.time() - 100000
    for it in items[:100]:
        if not isinstance(it, dict) or not isinstance(it.get("messages"), list): continue
        cid = uuid.uuid4().hex
        t += 10
        ex("INSERT INTO conversations(id,user_id,title,created,updated) VALUES(?,?,?,?,?)",
           (cid, u["id"], title_from(it.get("title") or DEFAULT_TITLE), t, t), commit=False)
        for m in it["messages"][:200]:
            if not isinstance(m, dict): continue
            body = str(m.get("text", "")).strip()[:20000]
            if body:
                t += 0.001
                ex("INSERT INTO messages(conv_id,role,body,created) VALUES(?,?,?,?)",
                   (cid, "user" if m.get("role") == "user" else "ai", body, t), commit=False)
        n += 1
    db().commit()
    return jsonify({"imported": n})

@app.get("/api/files/<fid>")
@login_required
def get_file(fid):
    row = q1("SELECT name,mime,kind,data FROM files WHERE id=? AND user_id=?", (fid, current_user()["id"]))
    if not row: return jsonify({"error": "Không tìm thấy tệp."}), 404
    inline = request.args.get("inline") == "1" and row["kind"] == "image" and row["mime"] in ("image/png", "image/jpeg", "image/webp")
    resp = send_file(io.BytesIO(bytes(row["data"])), mimetype=row["mime"] if inline else "application/octet-stream",
                     as_attachment=not inline, download_name=row["name"])
    resp.headers["Cache-Control"] = "private, max-age=3600" if inline else "no-store"
    return resp

# ------------------------------------------------------------------ tạo code mọi ngôn ngữ (một file hoặc zip, tối đa 50 MB)
FILE_RE = re.compile(r"^\s*={3,}\s*FILE:\s*(.+?)\s*={3,}\s*$")
END_RE = re.compile(r"^\s*={3,}\s*END\s*FILE\s*={3,}\s*$", re.I)
NOTE_RE = re.compile(r"^\s*={3,}\s*NOTE\s*={3,}\s*$", re.I)

def safe_path(p, used):
    p = str(p).replace("\\", "/").strip().strip("`'\"")
    parts = [re.sub(r"[^\w.\-+@()\[\] ]", "_", s).strip() for s in p.split("/")]
    parts = [s for s in parts if s and s not in (".", "..")][:12]
    name = "/".join(parts)[:200] or "file.txt"
    base, n = name, 1
    while name.lower() in used:
        n += 1
        stem, dot, ext = base.rpartition(".")
        name = f"{stem}_{n}.{ext}" if dot and stem else f"{base}_{n}"
    return name

def unfence(text):
    lines = text.split("\n")
    while lines and not lines[0].strip(): lines.pop(0)
    while lines and not lines[-1].strip(): lines.pop()
    if len(lines) >= 2 and re.match(r"^\s*```[\w+\-.#]*\s*$", lines[0]) and lines[-1].strip() == "```":
        lines = lines[1:-1]
    return "\n".join(lines)

def parse_bundle(text):
    """Tách phản hồi của AI thành (ghi chú, [(đường dẫn, nội dung)])."""
    note, raw, cur, state = [], [], None, "note"
    for line in text.replace("\r\n", "\n").split("\n"):
        m = FILE_RE.match(line)
        if m:
            if cur: raw.append(cur)
            cur, state = {"path": m.group(1), "lines": []}, "file"
            continue
        if END_RE.match(line) or NOTE_RE.match(line):
            if cur: raw.append(cur)
            cur, state = None, "note"
            continue
        (cur["lines"] if state == "file" and cur else note).append(line)
    if cur: raw.append(cur)
    files, used = [], set()
    for r in raw[:300]:
        path = safe_path(r["path"], used)
        used.add(path.lower())
        content = unfence("\n".join(r["lines"]))
        files.append((path, content + "\n" if content else ""))
    return "\n".join(note).strip(), files

def check_syntax(files):
    warns = []
    for path, content in files:
        low = path.lower()
        try:
            if low.endswith(".py"): ast.parse(content)
            elif low.endswith(".json"): json.loads(content)
        except SyntaxError as se:
            warns.append(f"{path}: lỗi cú pháp quanh dòng {se.lineno}")
        except ValueError as ve:
            warns.append(f"{path}: JSON không hợp lệ ({str(ve)[:60]})")
    return warns

def fmt_size(n):
    return f"{n/MB:.2f} MB" if n >= MB else f"{n/1024:.1f} KB"

def generate_code(contents, model_name):
    """Sinh code; nếu AI bị cắt giữa chừng thì tự yêu cầu viết tiếp (tối đa 6 lần) hoặc dừng khi chạm 50 MB."""
    cfg = types.GenerateContentConfig(system_instruction=CODE_SYSTEM, temperature=0.3, max_output_tokens=60000)
    text, rounds, convo, fin = "", 0, list(contents), ""
    while True:
        resp, _used = generate_with_retry(model_name, convo, cfg)
        piece = resp.text or ""
        text += piece
        rounds += 1
        try: fin = str(resp.candidates[0].finish_reason)
        except Exception: fin = ""
        if "MAX_TOKENS" not in fin or rounds >= 6 or len(text.encode("utf-8")) >= MAX_CODE_BYTES: break
        convo = convo + [types.Content(role="model", parts=[types.Part(text=piece)]),
                         types.Content(role="user", parts=[types.Part(text="Tiếp tục viết tiếp CHÍNH XÁC từ ký tự cuối cùng ở trên, giữ đúng định dạng === FILE ===. Không lặp lại phần đã viết.")])]
    return text, "MAX_TOKENS" in fin

def build_code_reply(text, cut_off):
    """Trả về (nội dung trả lời, [tệp cần lưu]) từ phản hồi thô của AI."""
    note, files = parse_bundle(text)
    if not files:
        return (text or "").strip() or "Chưa có nội dung trả về.", []
    total = sum(len(c.encode("utf-8")) for _, c in files)
    if total > MAX_CODE_BYTES:
        raise ValueError(f"Code tạo ra ({fmt_size(total)}) vượt giới hạn 50 MB. Hãy chia nhỏ yêu cầu.")
    stamp = time.strftime("%Y%m%d_%H%M%S")
    if len(files) == 1:
        name = posixpath.basename(files[0][0]) or "file.txt"
        data, mime = files[0][1].encode("utf-8"), "text/plain"
    else:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for p, c in files: zf.writestr(p, c)
        name, data, mime = f"huy_project_{stamp}.zip", buf.getvalue(), "application/zip"
    if len(data) > MAX_CODE_BYTES:
        raise ValueError("File sau khi đóng gói vượt giới hạn 50 MB. Hãy chia nhỏ yêu cầu.")
    lines = [note] if note else []
    if len(files) == 1:
        lines.append(f"✅ Đã tạo file **{name}** ({len(files[0][1].splitlines())} dòng, {fmt_size(len(data))}).")
    else:
        shown = ", ".join(p for p, _ in files[:25]) + (f" … (+{len(files)-25})" if len(files) > 25 else "")
        lines.append(f"✅ Đã tạo dự án **{len(files)} file**, đóng gói {name} ({fmt_size(len(data))}): {shown}")
    for w in check_syntax(files): lines.append("⚠ " + w + " — hãy nhắn lỗi vào chat này để mình sửa.")
    if cut_off: lines.append("⚠ Code quá dài nên có thể bị cắt dở ở cuối. Nhắn “viết tiếp” hoặc chia nhỏ yêu cầu.")
    lines.append("💾 File đã lưu trong cuộc trò chuyện này. Nếu chạy lỗi, cứ nhắn nội dung lỗi vào đây, mình sẽ sửa và gửi lại bản mới.")
    return "\n\n".join(lines), [{"name": name, "mime": mime, "kind": "code", "data": data}]

def code_context(conv_id, budget=300_000):
    """Nội dung các file code ở phiên bản MỚI NHẤT trong hội thoại, để AI sửa đúng trên bản đó."""
    m = q1("SELECT m.id AS id FROM messages m JOIN files f ON f.message_id=m.id WHERE m.conv_id=? AND f.kind='code' ORDER BY m.id DESC LIMIT 1", (conv_id,))
    if not m: return ""
    out, left = [], budget
    for f in q("SELECT name,data FROM files WHERE message_id=? AND kind='code'", (m["id"],)):
        data = bytes(f["data"])
        items = []
        if f["name"].lower().endswith(".zip"):
            try:
                with zipfile.ZipFile(io.BytesIO(data)) as zf:
                    for zi in zf.infolist():
                        if not zi.is_dir() and zi.file_size <= left: items.append((zi.filename, zf.read(zi)))
            except zipfile.BadZipFile:
                continue
        else:
            items.append((f["name"], data))
        for path, raw in items:
            try: txt = raw.decode("utf-8")
            except UnicodeDecodeError: continue
            if len(txt) > left: continue
            left -= len(txt)
            out.append(f"=== FILE: {path} ===\n{txt}\n=== END FILE ===")
    return "\n".join(out)

# ------------------------------------------------------------------ trang & tiện ích
@app.after_request
def security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(self), geolocation=()"
    if request.path.startswith("/api/") and "Cache-Control" not in response.headers:
        response.headers["Cache-Control"] = "no-store"
    return response

@app.get("/")
@limiter.exempt
def home():
    return send_from_directory("static", "index.html")

@app.get("/favicon.ico")
@limiter.exempt
def favicon():
    return "", 204

@app.get("/api/health")
@limiter.exempt
def health():
    return jsonify({"ok": True, "app": "Nguyễn Anh Huy AI", "configured": bool(SERVER_KEYS) or bool(db_keys()), "register": ALLOW_REGISTER})

def require_client():
    if not key_chain():
        return jsonify({"error": "Chưa có API key. Bấm biểu tượng ⚙ (Cài đặt) để dán Gemini API key của bạn."}), 503
    return None

def decode_attachment(att):
    if not isinstance(att, dict) or not att.get("data"):
        return None, None, None
    mime = str(att.get("mimeType", ""))
    if mime not in ("image/jpeg", "image/png", "image/webp", "application/pdf"):
        raise ValueError("Chỉ hỗ trợ ảnh JPG, PNG, WEBP và PDF.")
    try:
        raw = base64.b64decode(att["data"], validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("Dữ liệu tệp đính kèm không hợp lệ.")
    if len(raw) > 10 * MB:
        raise ValueError("Tệp tối đa 10 MB.")
    name = str(att.get("name", "attachment"))[:120]
    return mime, raw, name

# Model dự phòng khi model chính quá tải (503/500/504). Có thể đổi bằng biến GEMINI_FALLBACK_MODELS (cách nhau dấu phẩy)
FALLBACKS = [m.strip() for m in os.getenv("GEMINI_FALLBACK_MODELS", "gemini-2.5-flash,gemini-2.5-flash-lite").split(",") if m.strip()]

def is_overloaded(exc):
    m = str(exc).lower()
    return any(x in m for x in ("503", "unavailable", "overloaded", "high demand", "500", "internal", "504", "deadline"))

def generate_with_retry(primary, contents, config, fallbacks=None):
    """Thử từng API key; với mỗi key thử model chính 3 lần rồi model dự phòng.
    Lỗi hạn mức/key sai -> đổi sang key kế tiếp. Lỗi quá tải -> thử lại/đổi model."""
    models = [primary] + [m for m in (FALLBACKS if fallbacks is None else fallbacks) if m != primary]
    chain = key_chain()
    if not chain: raise RuntimeError("Chưa có API key.")
    last = None
    for ki, key in enumerate(chain):
        cl = client_for(key)
        key_failed = False
        for model in models:
            for attempt in range(3 if model == primary else 2):
                try:
                    resp = cl.models.generate_content(model=model, contents=contents, config=config)
                    return resp, model
                except Exception as exc:
                    last = exc
                    if is_key_error(exc) and not is_overloaded(exc):
                        _bad_until[key] = time.time() + 60
                        logging.warning("API key #%d lỗi (%s): %s -> thử key tiếp theo", ki + 1, model, str(exc)[:100])
                        key_failed = True
                        break
                    if not is_overloaded(exc):
                        raise
                    logging.warning("Model %s quá tải (lần %d): %s", model, attempt + 1, str(exc)[:120])
                    time.sleep(1.0 * (attempt + 1))
            if key_failed: break
    raise last

def build_history(conv_id, mode):
    """Dựng lịch sử hội thoại từ cơ sở dữ liệu (tối đa 16 tin gần nhất)."""
    rows = list(reversed(q("SELECT role,body FROM messages WHERE conv_id=? ORDER BY id DESC LIMIT 16", (conv_id,))))
    turns = []
    for r in rows:
        role = "user" if r["role"] == "user" else "model"
        txt = (r["body"] or "").strip()[:10000]
        if not txt: continue
        if turns and turns[-1][0] == role: turns[-1][1] += "\n\n" + txt
        else: turns.append([role, txt])
    while turns and turns[0][0] != "user": turns.pop(0)
    pending = ""
    if turns and turns[-1][0] == "user": pending = turns.pop()[1]   # tin trước đó chưa được trả lời (bị lỗi)
    contents = [types.Content(role=r, parts=[types.Part(text=t)]) for r, t in turns]
    recent_code = q1("""SELECT 1 AS x FROM files f JOIN (SELECT id FROM messages WHERE conv_id=? ORDER BY id DESC LIMIT 6) m
                        ON f.message_id=m.id WHERE f.kind='code' LIMIT 1""", (conv_id,))
    ctx = code_context(conv_id) if (mode == "code" or recent_code) else ""
    return contents, pending, ctx

@app.post("/api/chat")
@limiter.limit("30 per minute")
@login_required
def chat():
    err = require_client()
    if err: return err
    data = json_body()
    conv = get_conv(data.get("conversation_id"))
    if not conv: return jsonify({"error": "Không tìm thấy cuộc trò chuyện."}), 404
    message = str(data.get("message", "")).strip()
    raw_images = data.get("images") if isinstance(data.get("images"), list) else []
    if not message and not raw_images and not data.get("attachment"):
        return jsonify({"error": "Bạn chưa nhập nội dung."}), 400
    if not message: message = "Hãy đọc và giải bài trong tệp đính kèm."
    if len(message) > 600000: return jsonify({"error": "Tin nhắn quá dài (tối đa 600.000 ký tự)."}), 413
    display = str(data.get("display") or message).strip()[:20000] or message[:20000]
    mode = "code" if data.get("mode") == "code" else ""
    u = current_user()
    if not u["is_admin"] and user_storage(u["id"]) >= USER_QUOTA:
        return jsonify({"error": f"Tài khoản đã dùng hết {USER_QUOTA // MB} MB lưu trữ. Hãy xóa bớt cuộc trò chuyện cũ có file."}), 413

    try:
        contents, pending, ctx = build_history(conv["id"], mode)
        parts = []
        if ctx:
            parts.append(types.Part(text="[PHIÊN BẢN MỚI NHẤT của các file đã tạo trong cuộc trò chuyện này — dùng làm nền khi người dùng báo lỗi hoặc yêu cầu chỉnh sửa]\n" + ctx))
        if pending:
            parts.append(types.Part(text="(Tin nhắn trước đó chưa được trả lời): " + pending))
        parts.append(types.Part(text=message))
        for im in raw_images[:6]:
            m2, r2, _n = decode_attachment(im)
            if m2 and m2.startswith("image/"):
                parts.append(types.Part(inline_data=types.Blob(mime_type=m2, data=r2)))
        if raw_images:
            parts.append(types.Part(text="Hãy quan sát kỹ TẤT CẢ các ảnh đính kèm."))
        mime, raw, name = decode_attachment(data.get("attachment"))
        if mime and mime.startswith("image/"):
            parts.append(types.Part(inline_data=types.Blob(mime_type=mime, data=raw)))
            parts.append(types.Part(text="Hãy quan sát và phân tích hình ảnh đính kèm."))
        elif mime == "application/pdf":
            reader = PdfReader(io.BytesIO(raw))
            if len(reader.pages) > 35:
                return jsonify({"error": "PDF tối đa 35 trang trong một lần hỏi."}), 413
            extracted = "\n\n".join((page.extract_text() or "") for page in reader.pages)
            if not extracted.strip():
                return jsonify({"error": "Không trích xuất được chữ trong PDF. Có thể đây là PDF scan; hãy gửi ảnh trang hoặc PDF có OCR."}), 422
            parts.append(types.Part(text="Nội dung trích xuất từ PDF (" + name + "):\n" + extracted[:90000]))
        contents.append(types.Content(role="user", parts=parts))
        add_message(conv, "user", display)

        if mode == "code":
            text, cut_off = generate_code(contents, CHAT_MODEL)
            reply, files = build_code_reply(text, cut_off)
        else:
            config = types.GenerateContentConfig(system_instruction=SYSTEM, temperature=0.65, max_output_tokens=5000)
            if data.get("googleSearch") is True:
                config.tools = [types.Tool(google_search=types.GoogleSearch())]
            response, _used = generate_with_retry(CHAT_MODEL, contents, config)
            reply, files = response.text or "Chưa có nội dung trả về.", []
        mid, saved = add_message(conv, "ai", reply, files)
        return jsonify({"reply": reply, "files": saved, "title": conv["title"], "message_id": mid})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        logging.exception("Gemini chat failed")
        msg = str(exc).lower()
        if any(x in msg for x in ("quota", "429", "resource_exhausted")):
            return jsonify({"error": "Đã chạm hạn mức AI. Hãy kiểm tra trang quản lý API key và thử lại sau."}), 429
        if any(x in msg for x in ("api key", "unauthorized", "permission_denied", "401")):
            return jsonify({"error": "API key không hợp lệ hoặc chưa được cấp quyền."}), 401
        if data.get("googleSearch") is True and ("google_search" in msg or "tool" in msg):
            return jsonify({"error": "Tài khoản hiện chưa hỗ trợ tìm kiếm web. Hãy tắt nút Tìm web hoặc đổi model."}), 502
        if is_overloaded(exc):
            return jsonify({"error": "Máy chủ AI đang quá tải. Mình đã tự thử lại và đổi model dự phòng nhưng chưa được, bạn đợi 1-2 phút rồi gửi lại nhé."}), 503
        return jsonify({"error": "Trợ lý AI gặp lỗi: " + str(exc)[:350]}), 502

# ------------------------------------------------------------------ tải video / MP3 (YouTube, TikTok)
URL_RE = re.compile(r"^(https?://)?([a-zA-Z0-9-]+\.)?(youtube\.com|youtu\.be|tiktok\.com)/\S*$", re.I)
MAX_DL_BYTES = int(os.getenv("MAX_DOWNLOAD_MB", "150")) * MB
HAS_FFMPEG = bool(shutil.which("ffmpeg"))

def run_download(url, kind, outdir):
    import yt_dlp
    tmpl = os.path.join(outdir, "%(title).80s_%(id)s.%(ext)s")
    opts = {"outtmpl": tmpl, "quiet": True, "no_warnings": True, "noplaylist": True,
            "retries": 3, "fragment_retries": 3, "geo_bypass": True,
            "max_filesize": MAX_DL_BYTES,
            "extractor_args": {"youtube": {"player_client": ["android", "ios", "web"]}}}
    if kind == "audio":
        if HAS_FFMPEG:
            opts["format"] = "bestaudio/best"
            opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]
        else:
            opts["format"] = "bestaudio[ext=m4a]/bestaudio/best"
    else:
        if HAS_FFMPEG:
            opts["format"] = "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best"
            opts["merge_output_format"] = "mp4"
        else:
            opts["format"] = "best[height<=720][ext=mp4]/best[ext=mp4]/best"
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.extract_info(url, download=True)
    files = [f for f in os.listdir(outdir) if not f.endswith((".part", ".ytdl"))]
    if not files: return None
    files.sort(key=lambda f: os.path.getsize(os.path.join(outdir, f)), reverse=True)
    return os.path.join(outdir, files[0])

@app.post("/api/download")
@limiter.limit("5 per minute")
@login_required
def download():
    data = json_body()
    url = str(data.get("url", "")).strip()
    kind = "audio" if data.get("kind") == "audio" else "video"
    if not URL_RE.match(url):
        return jsonify({"error": "Chỉ hỗ trợ link YouTube hoặc TikTok."}), 400
    if not url.lower().startswith("http"): url = "https://" + url
    outdir = tempfile.mkdtemp(prefix="dl_")
    @after_this_request
    def cleanup(resp):
        shutil.rmtree(outdir, ignore_errors=True)
        return resp
    try:
        path = run_download(url, kind, outdir)
        if not path or not os.path.exists(path):
            return jsonify({"error": "Không tải được (video không khả dụng hoặc vượt giới hạn dung lượng)."}), 502
        if os.path.getsize(path) > MAX_DL_BYTES:
            return jsonify({"error": "File quá lớn so với giới hạn máy chủ."}), 413
        resp = send_file(path, as_attachment=True, download_name=os.path.basename(path))
        resp.headers["X-File-Name"] = base64.b64encode(os.path.basename(path).encode("utf-8")).decode("ascii")
        resp.headers["Access-Control-Expose-Headers"] = "X-File-Name"
        return resp
    except Exception as exc:
        logging.exception("Download failed")
        shutil.rmtree(outdir, ignore_errors=True)
        return jsonify({"error": "yt-dlp không tải được link này. Thử cập nhật yt-dlp hoặc link khác. Chi tiết: " + str(exc)[:200]}), 502

@app.post("/api/image")
@limiter.limit("15 per minute")
@login_required
def image():
    err = require_client()
    if err: return err
    data = json_body()
    conv = get_conv(data.get("conversation_id"))
    if not conv: return jsonify({"error": "Không tìm thấy cuộc trò chuyện."}), 404
    prompt = str(data.get("prompt", "")).strip()
    if not prompt: return jsonify({"error": "Hãy nhập mô tả ảnh."}), 400
    if len(prompt) > 3000: return jsonify({"error": "Mô tả ảnh tối đa 3.000 ký tự."}), 413
    u = current_user()
    if not u["is_admin"] and user_storage(u["id"]) >= USER_QUOTA:
        return jsonify({"error": f"Tài khoản đã dùng hết {USER_QUOTA // MB} MB lưu trữ. Hãy xóa bớt cuộc trò chuyện cũ có file."}), 413
    try:
        response, _ = generate_with_retry(
            IMAGE_MODEL, prompt,
            types.GenerateContentConfig(response_modalities=["TEXT", "IMAGE"]), fallbacks=[])
        for part in (response.candidates[0].content.parts if response.candidates else []):
            if getattr(part, "inline_data", None):
                blob = part.inline_data
                mime = blob.mime_type if blob.mime_type in ("image/png", "image/jpeg", "image/webp") else "image/png"
                ext = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}[mime]
                add_message(conv, "user", "🎨 Tạo ảnh: " + prompt)
                reply = "Đã tạo ảnh: " + prompt
                mid, saved = add_message(conv, "ai", reply, [{"name": f"huy_image_{time.strftime('%Y%m%d_%H%M%S')}.{ext}", "mime": mime, "kind": "image", "data": blob.data}])
                return jsonify({"reply": reply, "files": saved, "title": conv["title"], "message_id": mid})
        return jsonify({"error": "Model không trả về ảnh. Kiểm tra model tạo ảnh và quyền API."}), 502
    except Exception as exc:
        logging.exception("Gemini image generation failed")
        return jsonify({"error": "Không tạo được ảnh: " + str(exc)[:350]}), 502

# ------------------------------------------------------------------ admin

@app.get("/api/admin/keys")
@admin_required
def admin_keys_get():
    return jsonify({"saved": [mask_key(k) for k in db_keys()], "config": len(SERVER_KEYS)})

@app.post("/api/admin/keys")
@limiter.limit("30 per hour")
@admin_required
def admin_keys_set():
    raw = json_body().get("keys", "")
    keys = []
    for k in split_keys(raw):
        if k not in keys: keys.append(k)
    keys = keys[:10]
    if keys: ex("INSERT INTO settings(k,v) VALUES('gemini_keys',?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", ("\n".join(keys),))
    else: ex("DELETE FROM settings WHERE k='gemini_keys'")
    return jsonify({"saved": [mask_key(k) for k in keys]})

@app.get("/api/admin/users")
@admin_required
def admin_users():
    users = q("""SELECT u.id,u.email,u.name,u.is_admin,u.is_active,u.created,u.last_login,
        (SELECT COUNT(*) FROM conversations c WHERE c.user_id=u.id) AS convs,
        (SELECT COUNT(*) FROM messages m JOIN conversations c ON c.id=m.conv_id WHERE c.user_id=u.id) AS msgs,
        (SELECT COALESCE(SUM(size),0) FROM files f WHERE f.user_id=u.id) AS storage
        FROM users u ORDER BY u.created DESC""")
    stats = {"users": len(users), "convs": sum(x["convs"] for x in users), "msgs": sum(x["msgs"] for x in users),
             "storage": sum(x["storage"] for x in users), "register": ALLOW_REGISTER}
    return jsonify({"users": users, "stats": stats})

def target_user(uid):
    t = q1("SELECT * FROM users WHERE id=?", (uid,))
    if not t: return None, (jsonify({"error": "Không tìm thấy người dùng."}), 404)
    if t["is_admin"]: return None, (jsonify({"error": "Không thể thao tác trên tài khoản admin."}), 400)
    return t, None

@app.post("/api/admin/users/<int:uid>/active")
@admin_required
def admin_set_active(uid):
    t, err = target_user(uid)
    if err: return err
    active = 1 if json_body().get("active") else 0
    ex("UPDATE users SET is_active=?, token_ver=token_ver+1 WHERE id=?", (active, uid))
    return jsonify({"ok": True, "active": bool(active)})

@app.post("/api/admin/users/<int:uid>/password")
@admin_required
def admin_reset_password(uid):
    t, err = target_user(uid)
    if err: return err
    pw = str(json_body().get("password", ""))
    if not (8 <= len(pw) <= 128): return jsonify({"error": "Mật khẩu mới phải từ 8 đến 128 ký tự."}), 400
    ex("UPDATE users SET pw_hash=?, token_ver=token_ver+1, failed=0, locked_until=0 WHERE id=?", (generate_password_hash(pw), uid))
    return jsonify({"ok": True})

@app.delete("/api/admin/users/<int:uid>")
@admin_required
def admin_delete_user(uid):
    t, err = target_user(uid)
    if err: return err
    ex("DELETE FROM users WHERE id=?", (uid,))
    return jsonify({"ok": True})

@app.errorhandler(429)
def rate_limited(_):
    return jsonify({"error": "Bạn gửi quá nhanh. Vui lòng đợi một chút rồi thử lại."}), 429

@app.errorhandler(413)
def too_large(_):
    return jsonify({"error": "Yêu cầu quá lớn. Giới hạn tải lên của máy chủ là 40 MB."}), 413

init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
