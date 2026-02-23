

import os, hashlib, hmac, json, time, sqlite3, logging, secrets, re, queue, threading
from functools import wraps
from dotenv import load_dotenv
from flask import Flask, request, jsonify, send_from_directory, Response, stream_with_context
from urllib.parse import parse_qsl

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

BOT_TOKEN  = os.getenv("BOT_TOKEN", "")
ADMIN_IDS  = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]
ANON_SALT  = os.getenv("ANON_SALT", "change_me_please_use_random_string")
ADMIN_PASS = os.getenv("ADMIN_PASS", "bogdanm0011")
DB_PATH    = os.getenv("DB_PATH", "bot.db")
COOLDOWN   = int(os.getenv("COOLDOWN", "60"))
DEV_MODE   = os.getenv("DEV_MODE") == "1"
MAX_Q_LEN  = 1000
MIN_Q_LEN  = 3
SESSION_TTL = 3600 * 8  # 8 годин

app = Flask(__name__, static_folder=".", static_url_path="")

try:
    from flask_compress import Compress
    Compress(app)
except ImportError:
    pass

@app.after_request
def cors(r):
    r.headers["Access-Control-Allow-Origin"] = "*"
    r.headers["Access-Control-Allow-Headers"] = "Content-Type,X-Telegram-Init-Data,X-Admin-Token"
    r.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    return r

@app.route("/<path:p>", methods=["OPTIONS"])
def options(p): return "", 204


# ══════════════════════════════════════════════════════════════════════════════
#  DATABASE
# ══════════════════════════════════════════════════════════════════════════════

def get_db():
    db = sqlite3.connect(DB_PATH, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    return db


def init_db():
    with get_db() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS questions (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                anon_id      TEXT    NOT NULL,
                user_id      INTEGER NOT NULL,
                text         TEXT    NOT NULL,
                category     TEXT    DEFAULT 'Other',
                tags         TEXT    DEFAULT '',
                status       TEXT    DEFAULT 'pending',
                priority     INTEGER DEFAULT 0,
                read_at      INTEGER,
                created_at   INTEGER NOT NULL,
                answered_at  INTEGER,
                edit_deadline INTEGER
            );
            CREATE TABLE IF NOT EXISTS answers (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                question_id  INTEGER NOT NULL,
                text         TEXT    NOT NULL,
                publish_type TEXT    DEFAULT 'private',
                created_at   INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS public_wall (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                question_id   INTEGER NOT NULL,
                answer_id     INTEGER NOT NULL,
                question_text TEXT    NOT NULL,
                answer_text   TEXT    NOT NULL,
                created_at    INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS cooldowns (
                anon_id  TEXT    PRIMARY KEY,
                last_ask INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS blocked (
                anon_id    TEXT    PRIMARY KEY,
                blocked_at INTEGER NOT NULL,
                reason     TEXT    DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS ratings (
                question_id INTEGER PRIMARY KEY,
                value       INTEGER
            );
            CREATE TABLE IF NOT EXISTS admin_sessions (
                token      TEXT    PRIMARY KEY,
                created_at INTEGER NOT NULL,
                last_used  INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS drafts (
                anon_id  TEXT    PRIMARY KEY,
                text     TEXT    NOT NULL,
                category TEXT    DEFAULT 'Other',
                saved_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS templates (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                title      TEXT    NOT NULL,
                body       TEXT    NOT NULL,
                use_count  INTEGER DEFAULT 0,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS activity_log (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                hour_ts  INTEGER NOT NULL UNIQUE,
                count    INTEGER DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS rate_limit (
                key          TEXT    PRIMARY KEY,
                count        INTEGER DEFAULT 0,
                window_start INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS wall_reactions (
                wall_id    INTEGER NOT NULL,
                anon_id    TEXT    NOT NULL,
                emoji      TEXT    NOT NULL,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (wall_id, anon_id)
            );

            CREATE INDEX IF NOT EXISTS idx_q_anon    ON questions(anon_id);
            CREATE INDEX IF NOT EXISTS idx_q_status  ON questions(status);
            CREATE INDEX IF NOT EXISTS idx_q_created ON questions(created_at);
            CREATE INDEX IF NOT EXISTS idx_a_qid     ON answers(question_id);
            CREATE INDEX IF NOT EXISTS idx_wr_wall   ON wall_reactions(wall_id);
        """)

        # Міграції — безпечно при повторному запуску
        cols = [r[1] for r in db.execute("PRAGMA table_info(answers)").fetchall()]
        if "publish_type" not in cols:
            db.execute("ALTER TABLE answers ADD COLUMN publish_type TEXT DEFAULT 'private'")
            logger.info("Migration: added answers.publish_type")

        # Міграція: таблиця wall_reactions для існуючих БД
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        if "wall_reactions" not in tables:
            db.execute("""
                CREATE TABLE wall_reactions (
                    wall_id    INTEGER NOT NULL,
                    anon_id    TEXT    NOT NULL,
                    emoji      TEXT    NOT NULL,
                    created_at INTEGER NOT NULL,
                    PRIMARY KEY (wall_id, anon_id)
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS idx_wr_wall ON wall_reactions(wall_id)")
            logger.info("Migration: created wall_reactions table")

        # Шаблони за замовчуванням
        if db.execute("SELECT COUNT(*) FROM templates").fetchone()[0] == 0:
            now = int(time.time())
            db.executemany("INSERT INTO templates (title, body, created_at) VALUES (?,?,?)", [
                ("Дякую", "Дякую за твоє питання! 🙏", now),
                ("Обробляємо", "Твоє питання розглядається. Відповімо незабаром ⏳", now),
                ("Уточни", "Можеш уточнити деталі? 🤔", now),
            ])
    logger.info("DB ready ✓")


# ══════════════════════════════════════════════════════════════════════════════
#  RATE LIMITING
# ══════════════════════════════════════════════════════════════════════════════

def rate_limit_check(key: str, max_req: int = 60, window: int = 60) -> bool:
    now = int(time.time())
    window_start = now - window
    try:
        with get_db() as db:
            row = db.execute("SELECT count, window_start FROM rate_limit WHERE key=?", (key,)).fetchone()
            if not row or row["window_start"] < window_start:
                db.execute("INSERT OR REPLACE INTO rate_limit (key, count, window_start) VALUES (?,1,?)", (key, now))
                return True
            if row["count"] >= max_req:
                return False
            db.execute("UPDATE rate_limit SET count=count+1 WHERE key=?", (key,))
            return True
    except Exception:
        return True


def ip_rate_limit(max_req=30, window=60):
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            ip = request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",")[0].strip()
            if not rate_limit_check(f"ip:{ip}", max_req, window):
                return jsonify({"error": "rate_limited"}), 429
            return f(*args, **kwargs)
        return wrapper
    return decorator


# ══════════════════════════════════════════════════════════════════════════════
#  AUTH
# ══════════════════════════════════════════════════════════════════════════════

def verify_telegram_data(init_data: str):
    """Перевіряє підпис Telegram initData."""
    if not init_data:
        return None
    try:
        params = dict(parse_qsl(init_data, keep_blank_values=True))
        hash_val = params.pop("hash", "")
        data_check = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, hash_val):
            return None
        # Перевірка свіжості (7 днів)
        auth_date = int(params.get("auth_date", 0))
        if time.time() - auth_date > 86400 * 7:
            return None
        return json.loads(params.get("user", "{}"))
    except Exception as e:
        logger.error("Auth error: %s", e)
        return None


def require_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        # SSE передає initData як query param, решта — через header
        init_data = (
            request.headers.get("X-Telegram-Init-Data", "") or
            request.args.get("X-Telegram-Init-Data", "")
        )
        if DEV_MODE:
            # В DEV режимі використовуємо реальний initData якщо є, інакше дефолт
            user = verify_telegram_data(init_data)
            if not user:
                user = {"id": int(os.getenv("DEV_USER_ID", "12345")), "first_name": "Dev", "username": "dev"}
        else:
            user = verify_telegram_data(init_data)
            if not user:
                return jsonify({"error": "unauthorized"}), 401
        request.tg_user = user
        request.anon_id = hashlib.blake2b(
            f"{ANON_SALT}:{user['id']}".encode(), digest_size=16
        ).hexdigest()
        return f(*args, **kwargs)
    return wrapper


def create_admin_token() -> str:
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    with get_db() as db:
        db.execute("DELETE FROM admin_sessions WHERE created_at < ?", (now - SESSION_TTL,))
        db.execute("INSERT INTO admin_sessions (token, created_at, last_used) VALUES (?,?,?)", (token, now, now))
    return token


def verify_admin_token(token: str) -> bool:
    if not token:
        return False
    now = int(time.time())
    with get_db() as db:
        row = db.execute(
            "SELECT token FROM admin_sessions WHERE token=? AND created_at > ?",
            (token, now - SESSION_TTL)
        ).fetchone()
        if row:
            db.execute("UPDATE admin_sessions SET last_used=? WHERE token=?", (now, token))
            return True
    return False


def require_admin(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not verify_admin_token(request.headers.get("X-Admin-Token", "")):
            return jsonify({"error": "forbidden"}), 403
        return f(*args, **kwargs)
    return wrapper


# ══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def sanitize(text: str) -> str:
    return re.sub(r'<[^>]+>', '', text)


def check_cooldown(anon_id: str) -> int:
    with get_db() as db:
        row = db.execute("SELECT last_ask FROM cooldowns WHERE anon_id=?", (anon_id,)).fetchone()
    if not row:
        return 0
    return max(0, COOLDOWN - (int(time.time()) - row[0]))


def tg_send(chat_id: int, text: str):
    import urllib.request as ur
    payload = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "HTML"}).encode()
    try:
        req = ur.Request(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data=payload, headers={"Content-Type": "application/json"}
        )
        ur.urlopen(req, timeout=5)
    except Exception as e:
        logger.error("TG send to %s failed: %s", chat_id, e)


def notify_admins(text: str):
    for aid in ADMIN_IDS:
        tg_send(aid, text)


def send_answer_to_user(user_id: int, answer: str, question_id: int):
    tg_send(user_id, f"💬 <b>Відповідь на питання #{question_id}:</b>\n\n{answer}")


def log_activity():
    now = int(time.time())
    hour_ts = now - (now % 3600)
    try:
        with get_db() as db:
            db.execute(
                "INSERT INTO activity_log (hour_ts, count) VALUES (?, 1) ON CONFLICT(hour_ts) DO UPDATE SET count = count + 1",
                (hour_ts,)
            )
    except Exception:
        pass


# ── Stats cache ─────────────────────────────────────────────────────────────
_stats_cache = {}
_stats_cache_ts = 0
STATS_TTL = 30

def get_stats_cached():
    global _stats_cache, _stats_cache_ts
    now = int(time.time())
    if now - _stats_cache_ts < STATS_TTL and _stats_cache:
        return _stats_cache
    today = now - 86400
    with get_db() as db:
        q_today  = db.execute("SELECT COUNT(*) FROM questions WHERE created_at>=?", (today,)).fetchone()[0]
        total    = db.execute("SELECT COUNT(*) FROM questions WHERE status!='deleted'").fetchone()[0]
        pending  = db.execute("SELECT COUNT(*) FROM questions WHERE status='pending'").fetchone()[0]
        answered = db.execute("SELECT COUNT(*) FROM questions WHERE status='answered'").fetchone()[0]
        avg_row  = db.execute("SELECT AVG(answered_at-created_at) FROM questions WHERE status='answered' AND answered_at IS NOT NULL").fetchone()[0]
        wall_cnt = db.execute("SELECT COUNT(*) FROM public_wall").fetchone()[0]
        activity = db.execute("SELECT hour_ts, count FROM activity_log WHERE hour_ts >= ? ORDER BY hour_ts", (today,)).fetchall()
    _stats_cache = {
        "questions_today": q_today,
        "total": total,
        "pending": pending,
        "answered": answered,
        "wall_count": wall_cnt,
        "avg_response_sec": int(avg_row) if avg_row else 0,
        "activity": [{"h": r["hour_ts"], "c": r["count"]} for r in activity],
    }
    _stats_cache_ts = now
    return _stats_cache


# ══════════════════════════════════════════════════════════════════════════════
#  SSE
# ══════════════════════════════════════════════════════════════════════════════

_sse_clients: dict[str, list] = {}
_sse_lock = threading.Lock()


def sse_push(anon_id: str, event: str, data: dict):
    with _sse_lock:
        clients = _sse_clients.get(anon_id, [])
        msg = f"event: {event}\ndata: {json.dumps(data)}\n\n"
        for q in clients[:]:
            try:
                q.put_nowait(msg)
            except Exception:
                pass


@app.route("/api/sse")
@require_auth
def api_sse():
    anon_id = request.anon_id
    q = queue.Queue(maxsize=20)
    with _sse_lock:
        _sse_clients.setdefault(anon_id, []).append(q)

    def generate():
        try:
            yield f"event: ping\ndata: {{\"ts\":{int(time.time())}}}\n\n"
            while True:
                try:
                    msg = q.get(timeout=25)
                    yield msg
                except queue.Empty:
                    yield f"event: ping\ndata: {{\"ts\":{int(time.time())}}}\n\n"
        except GeneratorExit:
            pass
        finally:
            with _sse_lock:
                clients = _sse_clients.get(anon_id, [])
                if q in clients:
                    clients.remove(q)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"}
    )


# ══════════════════════════════════════════════════════════════════════════════
#  USER API
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return send_from_directory(".", "index.html")


@app.route("/api/me")
@require_auth
@ip_rate_limit(60, 60)
def api_me():
    uid = request.tg_user["id"]
    anon_id = request.anon_id
    wait = check_cooldown(anon_id)
    with get_db() as db:
        is_blocked = bool(db.execute("SELECT 1 FROM blocked WHERE anon_id=?", (anon_id,)).fetchone())
        draft = db.execute("SELECT text, category FROM drafts WHERE anon_id=?", (anon_id,)).fetchone()
        my_count = db.execute("SELECT COUNT(*) FROM questions WHERE anon_id=?", (anon_id,)).fetchone()[0]
    return jsonify({
        "is_admin": uid in ADMIN_IDS,
        "cooldown": wait,
        "is_blocked": is_blocked,
        "my_count": my_count,
        "draft": dict(draft) if draft else None,
    })


@app.route("/api/ask", methods=["POST"])
@require_auth
@ip_rate_limit(10, 60)
def api_ask():
    anon_id  = request.anon_id
    uid      = request.tg_user["id"]
    data     = request.get_json() or {}
    text     = sanitize((data.get("text") or "").strip())
    category = data.get("category", "Other")
    tags     = ",".join(t.strip()[:20] for t in (data.get("tags") or [])[:5] if t.strip())

    if not text or len(text) < MIN_Q_LEN:
        return jsonify({"error": "too_short"})
    if len(text) > MAX_Q_LEN:
        return jsonify({"error": "too_long"})

    with get_db() as db:
        if db.execute("SELECT 1 FROM blocked WHERE anon_id=?", (anon_id,)).fetchone():
            return jsonify({"error": "blocked"})

    wait = check_cooldown(anon_id)
    if wait > 0:
        return jsonify({"error": "cooldown", "wait": wait})

    now = int(time.time())
    edit_deadline = now + 120

    with get_db() as db:
        cur = db.execute(
            "INSERT INTO questions (anon_id, user_id, text, category, tags, created_at, edit_deadline) VALUES (?,?,?,?,?,?,?)",
            (anon_id, uid, text, category, tags, now, edit_deadline),
        )
        qid = cur.lastrowid
        db.execute("INSERT OR REPLACE INTO cooldowns (anon_id, last_ask) VALUES (?,?)", (anon_id, now))
        db.execute("DELETE FROM drafts WHERE anon_id=?", (anon_id,))

    log_activity()
    ts = time.strftime("%d.%m.%Y %H:%M", time.localtime(now))
    notify_admins(
        f"📩 <b>Нове питання #{qid}</b>\n"
        f"📂 {category}{' · 🏷 '+tags if tags else ''} · 🕐 {ts}\n\n"
        f"{text}\n\n<i>/reply_{qid}</i>"
    )
    return jsonify({"ok": True, "question_id": qid, "cooldown": COOLDOWN, "edit_deadline": edit_deadline})


@app.route("/api/edit/<int:question_id>", methods=["POST"])
@require_auth
def api_edit(question_id):
    anon_id = request.anon_id
    data = request.get_json() or {}
    text = sanitize((data.get("text") or "").strip())
    if not text or len(text) < MIN_Q_LEN or len(text) > MAX_Q_LEN:
        return jsonify({"error": "invalid"})
    now = int(time.time())
    with get_db() as db:
        q = db.execute(
            "SELECT edit_deadline FROM questions WHERE id=? AND anon_id=? AND status='pending'",
            (question_id, anon_id)
        ).fetchone()
        if not q:
            return jsonify({"error": "not_found"})
        if now > q["edit_deadline"]:
            return jsonify({"error": "too_late"})
        db.execute("UPDATE questions SET text=? WHERE id=?", (text, question_id))
    return jsonify({"ok": True})


@app.route("/api/draft", methods=["POST"])
@require_auth
def api_save_draft():
    anon_id = request.anon_id
    data = request.get_json() or {}
    text = (data.get("text") or "").strip()[:MAX_Q_LEN]
    category = data.get("category", "Other")
    if not text:
        with get_db() as db:
            db.execute("DELETE FROM drafts WHERE anon_id=?", (anon_id,))
        return jsonify({"ok": True})
    with get_db() as db:
        db.execute(
            "INSERT OR REPLACE INTO drafts (anon_id, text, category, saved_at) VALUES (?,?,?,?)",
            (anon_id, text, category, int(time.time()))
        )
    return jsonify({"ok": True})


@app.route("/api/my_questions")
@require_auth
def api_my_questions():
    """
    Повертає ТІЛЬКИ питання поточного юзера з їхніми відповідями.
    Фільтрація суворо по anon_id — інші юзери це НІКОЛИ не бачать.
    Відповіді — і private, і public (юзер бачить свої відповіді в будь-якому випадку).
    """
    anon_id = request.anon_id
    page = max(1, int(request.args.get("page", 1)))
    per = 20
    offset = (page - 1) * per

    with get_db() as db:
        rows = db.execute(
            """SELECT id, text, category, tags, status, read_at, created_at, answered_at
               FROM questions
               WHERE anon_id=? AND status != 'deleted'
               ORDER BY created_at DESC LIMIT ? OFFSET ?""",
            (anon_id, per, offset)
        ).fetchall()
        total = db.execute(
            "SELECT COUNT(*) FROM questions WHERE anon_id=? AND status != 'deleted'",
            (anon_id,)
        ).fetchone()[0]

    result = []
    for r in rows:
        q = dict(r)
        with get_db() as db:
            # Юзер бачить ВСІ свої відповіді (і private, і public)
            ans = db.execute(
                "SELECT text, created_at FROM answers WHERE question_id=? ORDER BY id ASC",
                (r["id"],)
            ).fetchall()
            rating = db.execute("SELECT value FROM ratings WHERE question_id=?", (r["id"],)).fetchone()
        q["answers"] = [{"text": a["text"], "ts": a["created_at"]} for a in ans]
        q["rated"] = bool(rating)
        result.append(q)

    return jsonify({"questions": result, "total": total, "page": page, "per": per})


@app.route("/api/rate", methods=["POST"])
@require_auth
@ip_rate_limit(20, 60)
def api_rate():
    data    = request.get_json() or {}
    qid     = data.get("question_id")
    val     = data.get("value")
    anon_id = request.anon_id
    if val not in (1, -1) or not qid:
        return jsonify({"error": "invalid"}), 400
    with get_db() as db:
        # Сувора перевірка — тільки власне питання
        if not db.execute(
            "SELECT 1 FROM questions WHERE id=? AND anon_id=? AND status='answered'",
            (qid, anon_id)
        ).fetchone():
            return jsonify({"error": "forbidden"}), 403
        db.execute("INSERT OR REPLACE INTO ratings (question_id, value) VALUES (?,?)", (qid, val))
    return jsonify({"ok": True})


@app.route("/api/wall")
@require_auth
@ip_rate_limit(60, 60)
def api_public_wall():
    """
    Публічна стінка — ТІЛЬКИ пари (питання + відповідь) які адмін явно
    позначив publish_type='public'. Звичайний юзер НІКОЛИ не бачить
    private-відповіді тут, навіть свої власні.
    """
    page = max(1, int(request.args.get("page", 1)))
    per = 20
    offset = (page - 1) * per
    anon_id = request.anon_id

    with get_db() as db:
        rows = db.execute(
            """SELECT id, question_text, answer_text, created_at
               FROM public_wall
               ORDER BY created_at DESC
               LIMIT ? OFFSET ?""",
            (per, offset)
        ).fetchall()
        total = db.execute("SELECT COUNT(*) FROM public_wall").fetchone()[0]

        items = []
        for r in rows:
            wall_id = r["id"]
            react_rows = db.execute(
                "SELECT emoji, COUNT(*) as cnt FROM wall_reactions WHERE wall_id=? GROUP BY emoji ORDER BY cnt DESC",
                (wall_id,)
            ).fetchall()
            my_react = db.execute(
                "SELECT emoji FROM wall_reactions WHERE wall_id=? AND anon_id=?",
                (wall_id, anon_id)
            ).fetchone()
            item = dict(r)
            item["reactions"] = {rr["emoji"]: rr["cnt"] for rr in react_rows}
            item["my_reaction"] = my_react["emoji"] if my_react else None
            items.append(item)

    return jsonify({
        "items": items,
        "total": total, "page": page, "per": per,
        "pages": max(1, (total + per - 1) // per)
    })


@app.route("/api/wall/react", methods=["POST"])
@require_auth
@ip_rate_limit(30, 60)
def api_wall_react():
    """
    Один юзер — одна реакція на пост стінки.
    Та сама emoji повторно — знімає реакцію.
    Інша emoji — замінює попередню.
    """
    anon_id = request.anon_id
    data    = request.get_json() or {}
    wall_id = data.get("wall_id")
    emoji   = (data.get("emoji") or "").strip()

    ALLOWED = {"❤️", "🔥", "😂", "🤯", "👏", "💔", "😮", "👀"}
    if not wall_id or emoji not in ALLOWED:
        return jsonify({"error": "invalid"}), 400

    now = int(time.time())
    with get_db() as db:
        if not db.execute("SELECT 1 FROM public_wall WHERE id=?", (wall_id,)).fetchone():
            return jsonify({"error": "not_found"}), 404

        existing = db.execute(
            "SELECT emoji FROM wall_reactions WHERE wall_id=? AND anon_id=?",
            (wall_id, anon_id)
        ).fetchone()

        if existing:
            if existing["emoji"] == emoji:
                db.execute("DELETE FROM wall_reactions WHERE wall_id=? AND anon_id=?", (wall_id, anon_id))
                my_reaction = None
            else:
                db.execute(
                    "UPDATE wall_reactions SET emoji=?, created_at=? WHERE wall_id=? AND anon_id=?",
                    (emoji, now, wall_id, anon_id)
                )
                my_reaction = emoji
        else:
            db.execute(
                "INSERT INTO wall_reactions (wall_id, anon_id, emoji, created_at) VALUES (?,?,?,?)",
                (wall_id, anon_id, emoji, now)
            )
            my_reaction = emoji

        react_rows = db.execute(
            "SELECT emoji, COUNT(*) as cnt FROM wall_reactions WHERE wall_id=? GROUP BY emoji ORDER BY cnt DESC",
            (wall_id,)
        ).fetchall()

    return jsonify({
        "ok": True,
        "reactions": {rr["emoji"]: rr["cnt"] for rr in react_rows},
        "my_reaction": my_reaction
    })


# ══════════════════════════════════════════════════════════════════════════════
#  ADMIN AUTH
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/admin/login", methods=["POST"])
@ip_rate_limit(5, 60)
def admin_login():
    data = request.get_json() or {}
    pwd  = data.get("password", "")
    if not pwd or not hmac.compare_digest(pwd, ADMIN_PASS):
        logger.warning("Failed admin login from %s", request.remote_addr)
        time.sleep(1)
        return jsonify({"error": "wrong_password"}), 403
    token = create_admin_token()
    return jsonify({"ok": True, "token": token, "expires_in": SESSION_TTL})


@app.route("/api/admin/logout", methods=["POST"])
def admin_logout():
    token = request.headers.get("X-Admin-Token", "")
    if token:
        with get_db() as db:
            db.execute("DELETE FROM admin_sessions WHERE token=?", (token,))
    return jsonify({"ok": True})


# ══════════════════════════════════════════════════════════════════════════════
#  ADMIN API
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/admin/stats")
@require_admin
def admin_stats():
    return jsonify(get_stats_cached())


@app.route("/api/admin/questions")
@require_admin
def admin_questions():
    page   = max(1, int(request.args.get("page", 1)))
    per    = min(int(request.args.get("per", 20)), 100)
    status = request.args.get("status", "pending")
    search = request.args.get("q", "").strip()
    offset = (page - 1) * per

    conditions = ["status != 'deleted'"]
    params = []

    if status and status != "all":
        conditions.append("status=?"); params.append(status)
    if search:
        conditions.append("text LIKE ?"); params.append(f"%{search}%")

    where = " AND ".join(conditions)
    with get_db() as db:
        rows = db.execute(
            f"SELECT id, anon_id, text, category, tags, status, priority, read_at, created_at FROM questions WHERE {where} ORDER BY priority DESC, created_at ASC LIMIT ? OFFSET ?",
            params + [per, offset]
        ).fetchall()
        total = db.execute(f"SELECT COUNT(*) FROM questions WHERE {where}", params).fetchone()[0]

    return jsonify({
        "questions": [dict(r) for r in rows],
        "total": total, "page": page, "per": per,
        "pages": max(1, (total + per - 1) // per)
    })


@app.route("/api/admin/reply", methods=["POST"])
@require_admin
def admin_reply():
    """
    Відповідь на питання.
    publish_type = 'private' → тільки цей юзер бачить у своїх відповідях
    publish_type = 'public'  → також з'являється на публічній стінці для всіх
    """
    data = request.get_json() or {}
    qid  = data.get("question_id")
    text = sanitize((data.get("text") or "").strip())
    publish_type = data.get("publish_type", "private")

    if not text:
        return jsonify({"error": "empty"})
    if publish_type not in ("private", "public"):
        publish_type = "private"

    now = int(time.time())
    with get_db() as db:
        q = db.execute("SELECT user_id, anon_id, text as qtext FROM questions WHERE id=? AND status!='deleted'", (qid,)).fetchone()
        if not q:
            return jsonify({"error": "not_found"})

        cur = db.execute(
            "INSERT INTO answers (question_id, text, publish_type, created_at) VALUES (?,?,?,?)",
            (qid, text, publish_type, now)
        )
        ans_id = cur.lastrowid
        db.execute("UPDATE questions SET status='answered', answered_at=?, read_at=COALESCE(read_at,?) WHERE id=?", (now, now, qid))

        if publish_type == "public":
            db.execute(
                "INSERT INTO public_wall (question_id, answer_id, question_text, answer_text, created_at) VALUES (?,?,?,?,?)",
                (qid, ans_id, q["qtext"], text, now)
            )

        tmpl_id = data.get("template_id")
        if tmpl_id:
            db.execute("UPDATE templates SET use_count=use_count+1 WHERE id=?", (tmpl_id,))

    send_answer_to_user(q["user_id"], text, qid)
    sse_push(q["anon_id"], "answer", {"question_id": qid, "text": text, "ts": now, "publish_type": publish_type})
    global _stats_cache_ts; _stats_cache_ts = 0

    logger.info("Reply #%s → user %s (%s)", qid, q["user_id"], publish_type)
    return jsonify({"ok": True})


@app.route("/api/admin/reply_extra", methods=["POST"])
@require_admin
def admin_reply_extra():
    """Додаткова відповідь (не змінює статус питання)."""
    data = request.get_json() or {}
    qid  = data.get("question_id")
    text = sanitize((data.get("text") or "").strip())
    publish_type = data.get("publish_type", "private")
    if not text:
        return jsonify({"error": "empty"})
    if publish_type not in ("private", "public"):
        publish_type = "private"

    now = int(time.time())
    with get_db() as db:
        q = db.execute("SELECT user_id, anon_id, text as qtext FROM questions WHERE id=?", (qid,)).fetchone()
        if not q:
            return jsonify({"error": "not_found"})
        cur = db.execute(
            "INSERT INTO answers (question_id, text, publish_type, created_at) VALUES (?,?,?,?)",
            (qid, text, publish_type, now)
        )
        if publish_type == "public":
            db.execute(
                "INSERT INTO public_wall (question_id, answer_id, question_text, answer_text, created_at) VALUES (?,?,?,?,?)",
                (qid, cur.lastrowid, q["qtext"], text, now)
            )

    send_answer_to_user(q["user_id"], text, qid)
    sse_push(q["anon_id"], "answer", {"question_id": qid, "text": text, "ts": now, "publish_type": publish_type})
    return jsonify({"ok": True})


@app.route("/api/admin/mark_read", methods=["POST"])
@require_admin
def admin_mark_read():
    qid = (request.get_json() or {}).get("question_id")
    with get_db() as db:
        db.execute("UPDATE questions SET read_at=? WHERE id=? AND read_at IS NULL", (int(time.time()), qid))
    return jsonify({"ok": True})


@app.route("/api/admin/priority", methods=["POST"])
@require_admin
def admin_priority():
    data = request.get_json() or {}
    qid  = data.get("question_id")
    val  = 1 if data.get("starred") else 0
    with get_db() as db:
        db.execute("UPDATE questions SET priority=? WHERE id=?", (val, qid))
    return jsonify({"ok": True})


@app.route("/api/admin/delete", methods=["POST"])
@require_admin
def admin_delete():
    qid = (request.get_json() or {}).get("question_id")
    with get_db() as db:
        db.execute("UPDATE questions SET status='deleted' WHERE id=?", (qid,))
    global _stats_cache_ts; _stats_cache_ts = 0
    return jsonify({"ok": True})


@app.route("/api/admin/block", methods=["POST"])
@require_admin
def admin_block():
    data    = request.get_json() or {}
    anon_id = data.get("anon_id")
    qid     = data.get("question_id")
    reason  = data.get("reason", "")[:200]
    with get_db() as db:
        db.execute("INSERT OR IGNORE INTO blocked (anon_id, blocked_at, reason) VALUES (?,?,?)", (anon_id, int(time.time()), reason))
        if qid:
            db.execute("UPDATE questions SET status='deleted' WHERE id=?", (qid,))
    return jsonify({"ok": True})


@app.route("/api/admin/unblock", methods=["POST"])
@require_admin
def admin_unblock():
    anon_id = (request.get_json() or {}).get("anon_id")
    with get_db() as db:
        db.execute("DELETE FROM blocked WHERE anon_id=?", (anon_id,))
    return jsonify({"ok": True})


# ── Діалоги по юзерах ────────────────────────────────────────────────────────

@app.route("/api/admin/users")
@require_admin
def admin_users():
    """Список всіх юзерів що писали питання (для вкладки Діалоги)."""
    with get_db() as db:
        rows = db.execute("""
            SELECT q.anon_id,
                   COUNT(q.id)                                              as q_count,
                   SUM(CASE WHEN q.status='answered' THEN 1 ELSE 0 END)    as answered,
                   SUM(CASE WHEN q.status='pending'  THEN 1 ELSE 0 END)    as pending,
                   MAX(q.created_at)                                        as last_at
            FROM questions q
            WHERE q.status != 'deleted'
            GROUP BY q.anon_id
            ORDER BY last_at DESC
        """).fetchall()
    return jsonify({"users": [dict(r) for r in rows]})


@app.route("/api/admin/dialog/<anon_id>")
@require_admin
def admin_dialog(anon_id):
    """Вся переписка з конкретним юзером — питання + відповіді в хронологічному порядку."""
    with get_db() as db:
        questions = db.execute(
            "SELECT id, text, status, created_at FROM questions WHERE anon_id=? AND status != 'deleted' ORDER BY created_at ASC",
            (anon_id,)
        ).fetchall()
        result = []
        for q in questions:
            qdict = dict(q)
            answers = db.execute(
                "SELECT id, text, publish_type, created_at FROM answers WHERE question_id=? ORDER BY id ASC",
                (q["id"],)
            ).fetchall()
            qdict["answers"] = [dict(a) for a in answers]
            result.append(qdict)
    return jsonify({"dialog": result, "anon_id": anon_id})


@app.route("/api/admin/send_to_user", methods=["POST"])
@require_admin
def admin_send_to_user():
    """Написати юзеру напряму з діалогу (прикріплюється до останнього питання)."""
    data = request.get_json() or {}
    anon_id = data.get("anon_id")
    text = sanitize((data.get("text") or "").strip())
    publish_type = data.get("publish_type", "private")
    if not anon_id or not text:
        return jsonify({"error": "invalid"})
    if publish_type not in ("private", "public"):
        publish_type = "private"

    now = int(time.time())
    with get_db() as db:
        q = db.execute(
            "SELECT id, user_id, text as qtext FROM questions WHERE anon_id=? AND status!='deleted' ORDER BY created_at DESC LIMIT 1",
            (anon_id,)
        ).fetchone()
        if not q:
            return jsonify({"error": "user_not_found"})
        cur = db.execute(
            "INSERT INTO answers (question_id, text, publish_type, created_at) VALUES (?,?,?,?)",
            (q["id"], text, publish_type, now)
        )
        if publish_type == "public":
            db.execute(
                "INSERT INTO public_wall (question_id, answer_id, question_text, answer_text, created_at) VALUES (?,?,?,?,?)",
                (q["id"], cur.lastrowid, q["qtext"], text, now)
            )

    send_answer_to_user(q["user_id"], text, q["id"])
    sse_push(anon_id, "answer", {"question_id": q["id"], "text": text, "ts": now, "publish_type": publish_type})
    return jsonify({"ok": True})


# ── Templates ─────────────────────────────────────────────────────────────────

@app.route("/api/admin/templates")
@require_admin
def admin_get_templates():
    with get_db() as db:
        rows = db.execute("SELECT * FROM templates ORDER BY use_count DESC").fetchall()
    return jsonify({"templates": [dict(r) for r in rows]})


@app.route("/api/admin/templates", methods=["POST"])
@require_admin
def admin_save_template():
    data  = request.get_json() or {}
    title = sanitize((data.get("title") or "").strip())[:100]
    body  = sanitize((data.get("body") or "").strip())[:500]
    tid   = data.get("id")
    if not title or not body:
        return jsonify({"error": "invalid"})
    now = int(time.time())
    with get_db() as db:
        if tid:
            db.execute("UPDATE templates SET title=?, body=? WHERE id=?", (title, body, tid))
            return jsonify({"ok": True, "id": tid})
        cur = db.execute("INSERT INTO templates (title, body, created_at) VALUES (?,?,?)", (title, body, now))
    return jsonify({"ok": True, "id": cur.lastrowid})


# ── Export ────────────────────────────────────────────────────────────────────

@app.route("/api/admin/export")
@require_admin
def admin_export():
    import csv, io
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "category", "text", "status", "created_at", "answered_at"])
    with get_db() as db:
        rows = db.execute("SELECT id,category,text,status,created_at,answered_at FROM questions WHERE status!='deleted' ORDER BY id").fetchall()
    for r in rows:
        writer.writerow([
            r["id"], r["category"], r["text"], r["status"],
            time.strftime("%Y-%m-%d %H:%M", time.localtime(r["created_at"])),
            time.strftime("%Y-%m-%d %H:%M", time.localtime(r["answered_at"])) if r["answered_at"] else ""
        ])
    output.seek(0)
    return Response(output.read(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=questions.csv"})


# ── Webhook (Telegram bot replies) ────────────────────────────────────────────

_pending_reply: dict[int, int] = {}

@app.route("/webhook", methods=["POST"])
def webhook():
    update = request.get_json() or {}
    msg = update.get("message", {})
    text = msg.get("text", "")
    from_id = msg.get("from", {}).get("id")

    if text == "/start":
        tg_send(from_id, "👋 Привіт! Натисни кнопку «Меню» внизу зліва ↙️ щоб відкрити додаток")
        return "ok"

    if from_id not in ADMIN_IDS:
        return "ok"

    if text.startswith("/reply_"):
        try:
            qid = int(text.split("_")[1])
            _pending_reply[from_id] = qid
            tg_send(from_id, f"✍️ Send reply for question #{qid}:")
        except Exception:
            pass
    elif from_id in _pending_reply:
        qid = _pending_reply.pop(from_id)
        reply_text = sanitize(text)
        now = int(time.time())
        with get_db() as db:
            q = db.execute("SELECT user_id, anon_id FROM questions WHERE id=?", (qid,)).fetchone()
            if q:
                db.execute("INSERT INTO answers (question_id, text, publish_type, created_at) VALUES (?,?,'private',?)", (qid, reply_text, now))
                db.execute("UPDATE questions SET status='answered', answered_at=? WHERE id=?", (now, qid))
                send_answer_to_user(q["user_id"], reply_text, qid)
                sse_push(q["anon_id"], "answer", {"question_id": qid, "text": reply_text, "ts": now, "publish_type": "private"})
                tg_send(from_id, f"✅ Reply sent for #{qid}!")
    return "ok"


# ── Health ────────────────────────────────────────────────────────────────────

@app.route("/api/health")
def health():
    return jsonify({"ok": True, "ts": int(time.time()), "version": "3.0"})


if __name__ == "__main__":
    init_db()
    port = int(os.getenv("PORT", 5000))
    logger.info("🚀 Silence-Net: AnonimQ v3.0 on port %s (DEV=%s)", port, DEV_MODE)
    app.run(host="0.0.0.0", port=port, debug=DEV_MODE, threaded=True)
