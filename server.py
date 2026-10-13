#!/usr/bin/env python3
"""Local server for Strong Coin Radar.

Serves the page and stores each customer's workspace in SQLite.
No third-party account is required.
"""

import hashlib
import json
import os
import secrets
import sqlite3
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "radar.sqlite"
HTML_NAME = "strong-coin-radar-v3.3.1.html"
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8787"))
COOKIE = "radar_session"
SESSION_DAYS = 14
MAX_BODY = 1_500_000
SORT_KEYS = {"totalReturn", "return30d", "winRate", "maxDrawdown", "trades", "profitFactor"}
HOT_PUSH_URL = os.environ.get("HOT_PUSH_URL", "https://alpha.c7.pro/bot/receive_hot").strip()
HOT_PUSH_COOLDOWN_HOURS = max(1, int(os.environ.get("HOT_PUSH_COOLDOWN_HOURS", "6")))
HOT_PUSH_TIMEOUT = max(10, int(os.environ.get("HOT_PUSH_TIMEOUT", "60")))
HOT_PUSH_MIN_SCORE = float(os.environ.get("HOT_PUSH_MIN_SCORE", "75"))
HOT_PUSH_MIN_CHANGE = float(os.environ.get("HOT_PUSH_MIN_CHANGE", "3"))
HOT_PUSH_MIN_RVOL = float(os.environ.get("HOT_PUSH_MIN_RVOL", "1.2"))
HOT_PUSH_MIN_VOLUME_M = float(os.environ.get("HOT_PUSH_MIN_VOLUME_M", "5"))
HOT_PUSH_MAX_PER_SCAN = max(1, min(30, int(os.environ.get("HOT_PUSH_MAX_PER_SCAN", "12"))))
_hot_push_lock = threading.Lock()
_hot_push_inflight = set()


def hot_push_config():
    return {
        "url": HOT_PUSH_URL,
        "cooldownHours": HOT_PUSH_COOLDOWN_HOURS,
        "timeout": HOT_PUSH_TIMEOUT,
        "minScore": HOT_PUSH_MIN_SCORE,
        "minChange": HOT_PUSH_MIN_CHANGE,
        "minRvol": HOT_PUSH_MIN_RVOL,
        "minVolumeM": HOT_PUSH_MIN_VOLUME_M,
        "maxPerScan": HOT_PUSH_MAX_PER_SCAN,
        "enabled": bool(HOT_PUSH_URL),
    }


def metric_float(value, default=0.0):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number or number in (float("inf"), float("-inf")):
        return default
    return number


def select_hot_symbols(payload):
    """Accept either symbols=[], or coins=[{symbol,score,change,rvol,quoteVolume}]."""
    coins = payload.get("coins")
    if isinstance(coins, list):
        ranked = []
        min_volume = HOT_PUSH_MIN_VOLUME_M * 1_000_000
        for item in coins:
            if not isinstance(item, dict):
                continue
            symbol = normalize_hot_symbol(item.get("symbol") or "")
            if not symbol:
                continue
            score = metric_float(item.get("score"))
            change = metric_float(item.get("change"))
            rvol = metric_float(item.get("rvol"))
            volume = metric_float(item.get("quoteVolume") if item.get("quoteVolume") is not None else item.get("volume"))
            if score < HOT_PUSH_MIN_SCORE:
                continue
            if change < HOT_PUSH_MIN_CHANGE:
                continue
            if rvol < HOT_PUSH_MIN_RVOL:
                continue
            if volume < min_volume:
                continue
            ranked.append((score, change, rvol, volume, symbol))
        ranked.sort(key=lambda row: (row[0], row[1], row[2], row[3]), reverse=True)
        out, seen = [], set()
        for *_, symbol in ranked:
            if symbol in seen:
                continue
            seen.add(symbol)
            out.append(symbol)
            if len(out) >= HOT_PUSH_MAX_PER_SCAN:
                break
        return out

    symbols = payload.get("symbols")
    if symbols is None and payload.get("symbol") is not None:
        symbols = [payload.get("symbol")]
    if not isinstance(symbols, list):
        return None
    return clean_hot_symbols(symbols)[:HOT_PUSH_MAX_PER_SCAN]


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_iso(value):
    return datetime.fromisoformat(value)


def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
              id INTEGER PRIMARY KEY,
              username TEXT NOT NULL UNIQUE COLLATE NOCASE,
              password_hash TEXT NOT NULL,
              display_name TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
              token_hash TEXT PRIMARY KEY,
              user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              expires_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS user_states (
              user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
              state_json TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS strategies (
              id INTEGER PRIMARY KEY,
              user_id INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
              snapshot_json TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS hot_pushes (
              symbol TEXT PRIMARY KEY COLLATE NOCASE,
              pushed_at TEXT NOT NULL,
              last_status TEXT,
              last_error TEXT
            );
            """
        )


def normalize_hot_symbol(raw):
    if not isinstance(raw, str):
        return ""
    name = raw.strip().upper().replace("/", "").replace("-", "")
    if name.endswith("USDT"):
        name = name[:-4]
    if len(name) < 2 or len(name) > 20:
        return ""
    if not all(ch.isalnum() for ch in name):
        return ""
    return name


def hot_push_due(conn, symbol):
    row = conn.execute("SELECT pushed_at FROM hot_pushes WHERE symbol = ?", (symbol,)).fetchone()
    if not row:
        return True
    try:
        last = parse_iso(row["pushed_at"])
    except ValueError:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - last >= timedelta(hours=HOT_PUSH_COOLDOWN_HOURS)


def forward_hot_symbol(symbol):
    if not HOT_PUSH_URL:
        return False, "未配置 HOT_PUSH_URL"
    body = urllib.parse.urlencode({"symbol": symbol}).encode("utf-8")
    req = urllib.request.Request(
        HOT_PUSH_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "StrongCoinRadar/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=HOT_PUSH_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        return False, f"HTTP {exc.code}: {raw[:200]}"
    except Exception as exc:
        return False, str(exc)
    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        payload = {}
    ok = status < 400 and str(payload.get("status", "1")) in {"1", "true", "True", "ok"}
    if ok or (status < 400 and "接收成功" in raw):
        return True, raw[:240] or "ok"
    return False, raw[:240] or f"HTTP {status}"


def clean_hot_symbols(symbols):
    cleaned, seen = [], set()
    for item in symbols or []:
        name = normalize_hot_symbol(item)
        if not name or name in seen:
            continue
        seen.add(name)
        cleaned.append(name)
    return cleaned


def mark_hot_push(symbol, ok, detail):
    with db() as conn:
        if ok:
            conn.execute(
                """
                INSERT INTO hot_pushes (symbol, pushed_at, last_status, last_error)
                VALUES (?, ?, ?, NULL)
                ON CONFLICT(symbol) DO UPDATE SET
                  pushed_at = excluded.pushed_at,
                  last_status = excluded.last_status,
                  last_error = NULL
                """,
                (symbol, now_iso(), detail),
            )
        else:
            conn.execute(
                """
                INSERT INTO hot_pushes (symbol, pushed_at, last_status, last_error)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                  last_status = excluded.last_status,
                  last_error = excluded.last_error
                """,
                (symbol, "1970-01-01T00:00:00+00:00", "failed", detail),
            )
        conn.commit()


def queue_hot_symbols(symbols):
    cleaned = clean_hot_symbols(symbols)[:12]
    queued, skipped = [], []
    with _hot_push_lock:
        with db() as conn:
            for symbol in cleaned:
                if symbol in _hot_push_inflight:
                    skipped.append({"symbol": symbol, "reason": "pushing"})
                    continue
                if not hot_push_due(conn, symbol):
                    skipped.append({"symbol": symbol, "reason": "cooldown"})
                    continue
                _hot_push_inflight.add(symbol)
                queued.append(symbol)
    if queued:
        threading.Thread(target=_push_hot_worker, args=(list(queued),), daemon=True).start()
    return {"queued": queued, "skipped": skipped, "cooldownHours": HOT_PUSH_COOLDOWN_HOURS}


def _push_hot_worker(symbols):
    for symbol in symbols:
        try:
            ok, detail = forward_hot_symbol(symbol)
            mark_hot_push(symbol, ok, detail)
            print(f"[hot-push] {'ok' if ok else 'fail'} {symbol} {detail[:120]}")
        except Exception as exc:
            mark_hot_push(symbol, False, str(exc))
            print(f"[hot-push] error {symbol} {exc}")
        finally:
            with _hot_push_lock:
                _hot_push_inflight.discard(symbol)


def hash_password(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
    return f"{salt.hex()}:{digest.hex()}"


def check_password(password, stored):
    try:
        salt_hex, digest_hex = stored.split(":", 1)
        salt = bytes.fromhex(salt_hex)
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
    return secrets.compare_digest(digest.hex(), digest_hex)


def token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def valid_username(name):
    if not isinstance(name, str):
        return False
    name = name.strip()
    if len(name) < 2 or len(name) > 32:
        return False
    return all(ch.isalnum() or ch in "_-" for ch in name)


def public_user(row):
    return {"id": row["id"], "username": row["username"], "displayName": row["display_name"]}


def read_cookie(header):
    if not header:
        return None
    for part in header.split(";"):
        if "=" not in part:
            continue
        key, value = part.strip().split("=", 1)
        if key == COOKIE:
            return value
    return None


def current_user(conn, handler):
    token = read_cookie(handler.headers.get("Cookie"))
    if not token:
        return None
    row = conn.execute(
        """
        SELECT u.id, u.username, u.display_name, s.expires_at
        FROM sessions s JOIN users u ON u.id = s.user_id
        WHERE s.token_hash = ?
        """,
        (token_hash(token),),
    ).fetchone()
    if not row:
        return None
    if parse_iso(row["expires_at"]) <= datetime.now(timezone.utc):
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash(token),))
        conn.commit()
        return None
    return row


def issue_session(conn, user_id):
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)
    conn.execute(
        "INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
        (token_hash(token), user_id, expires.replace(microsecond=0).isoformat()),
    )
    conn.commit()
    return token


def clear_session(conn, handler):
    token = read_cookie(handler.headers.get("Cookie"))
    if token:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash(token),))
        conn.commit()


def header_hosts(value):
    return [part.strip().lower() for part in (value or "").split(",") if part.strip()]


def hostname_only(value):
    value = value.strip().lower()
    if value.startswith("[") and "]" in value:
        return value[1:value.index("]")]
    if value.count(":") == 1:
        return value.rsplit(":", 1)[0]
    return value


def loopback_name(value):
    return hostname_only(value) in {"127.0.0.1", "localhost", "::1"}


def request_hosts(handler):
    hosts = []
    for name in ("X-Forwarded-Host", "X-Host", "Host"):
        hosts.extend(header_hosts(handler.headers.get(name)))
    return hosts


def public_host(handler):
    hosts = request_hosts(handler)
    for host in hosts:
        if not loopback_name(host):
            return host
    return hosts[0] if hosts else ""


def same_origin(handler):
    origin = handler.headers.get("Origin")
    if not origin:
        return True
    parsed = urlparse(origin)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    origin_name = parsed.hostname.lower()
    hosts = request_hosts(handler)
    public = [host for host in hosts if not loopback_name(host)]
    for host in public or hosts:
        if hostname_only(host) == origin_name:
            return True
    # 宝塔反代常把 Host 改成 127.0.0.1，浏览器带来的域名对不上。
    # 这时请求只来自本机 Nginx，登录口令本身仍由 SameSite=Lax 保护。
    peer = handler.client_address[0] if handler.client_address else ""
    return bool(hosts) and not public and peer in {"127.0.0.1", "::1"}


def https_request(handler):
    proto = (handler.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
    return proto == "https"


def metric_number(value, default=0.0):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number or number in (float("inf"), float("-inf")):
        return default
    return number


def strategy_view(row, include_config=False):
    try:
        snap = json.loads(row["snapshot_json"])
    except json.JSONDecodeError:
        snap = {}
    metrics = snap.get("metrics") if isinstance(snap.get("metrics"), dict) else {}
    item = {
        "id": str(row["id"]),
        "strategyName": str(snap.get("strategyName") or "Strategy")[:40],
        "ownerName": str(snap.get("ownerName") or "—")[:24],
        "strategyType": str(snap.get("strategyType") or "momentum-v1")[:80],
        "totalReturn": metric_number(metrics.get("totalReturn")),
        "return30d": metric_number(metrics.get("return30d")),
        "winRate": metric_number(metrics.get("winRate")),
        "maxDrawdown": abs(metric_number(metrics.get("maxDrawdown"))),
        "trades": max(0, int(metric_number(metrics.get("trades")))),
        "profitFactor": metric_number(metrics.get("profitFactor")) if metrics.get("profitFactor") is not None else None,
        "followers": 0,
        "verified": False,
        "memberOnly": True,
        "updatedAt": row["updated_at"],
    }
    if include_config and isinstance(snap.get("config"), dict):
        item["config"] = snap["config"]
    return item


class Handler(BaseHTTPRequestHandler):
    server_version = "StrongCoinRadar/1.0"

    def log_message(self, fmt, *args):
        print(f"[{self.log_date_time_string()}] {self.address_string()} {fmt % args}")

    def send_json(self, payload, status=200, cookie=None, clear_cookie=False):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        secure = "; Secure" if https_request(self) else ""
        if cookie:
            self.send_header(
                "Set-Cookie",
                f"{COOKIE}={cookie}; HttpOnly; SameSite=Lax; Path=/; Max-Age={SESSION_DAYS * 86400}{secure}",
            )
        if clear_cookie:
            self.send_header("Set-Cookie", f"{COOKIE}=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0{secure}")
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, status, message):
        self.send_json({"error": message}, status)

    def read_json(self):
        if not same_origin(self):
            self.send_error_json(403, "请求来源不正确")
            return None
        length = int(self.headers.get("Content-Length") or 0)
        if length < 0 or length > MAX_BODY:
            self.send_error_json(413, "提交内容太大")
            return None
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_error_json(400, "请求不是有效的 JSON")
            return None
        if not isinstance(data, dict):
            self.send_error_json(400, "请求格式不正确")
            return None
        return data

    def do_GET(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        if path == "/api/health":
            self.send_json({"ok": True})
            return
        if path == "/api/hot/config":
            self.send_json({"ok": True, "config": hot_push_config()})
            return
        if path == "/api/auth/me":
            self.handle_me()
            return
        if path == "/api/me/state":
            self.handle_get_state()
            return
        if path == "/strategies/leaderboard":
            self.handle_leaderboard(parse_qs(parsed.query))
            return
        if path.startswith("/strategies/") and path != "/strategies/me/snapshot":
            self.handle_strategy_detail(path.rsplit("/", 1)[-1])
            return
        if path == f"/{HTML_NAME}":
            target = "/" + (f"?{parsed.query}" if parsed.query else "")
            self.send_response(302)
            self.send_header("Location", target)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        if path == "/":
            self.serve_html()
            return
        self.send_error_json(404, "没有这个地址")

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/auth/register":
            self.handle_register()
            return
        if path == "/api/auth/login":
            self.handle_login()
            return
        if path == "/api/auth/logout":
            self.handle_logout()
            return
        if path == "/strategies/me/snapshot":
            self.handle_snapshot()
            return
        if path == "/api/hot/push":
            self.handle_hot_push()
            return
        self.send_error_json(404, "没有这个地址")

    def do_PUT(self):
        if urlparse(self.path).path == "/api/me/state":
            self.handle_put_state()
            return
        self.send_error_json(404, "没有这个地址")

    def serve_html(self):
        target = ROOT / HTML_NAME
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def handle_register(self):
        data = self.read_json()
        if data is None:
            return
        username = str(data.get("username") or "").strip()
        password = str(data.get("password") or "")
        display = str(data.get("displayName") or username).strip()[:32] or username
        if not valid_username(username):
            self.send_error_json(400, "用户名需要 2-32 位字母、数字或中文")
            return
        if len(password) < 6 or len(password) > 72:
            self.send_error_json(400, "密码至少 6 位")
            return
        with db() as conn:
            exists = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
            if exists:
                self.send_error_json(409, "这个用户名已经注册过了")
                return
            cur = conn.execute(
                "INSERT INTO users (username, password_hash, display_name, created_at) VALUES (?, ?, ?, ?)",
                (username, hash_password(password), display, now_iso()),
            )
            conn.commit()
            user = conn.execute("SELECT id, username, display_name FROM users WHERE id = ?", (cur.lastrowid,)).fetchone()
            token = issue_session(conn, user["id"])
        self.send_json({"user": public_user(user)}, 201, cookie=token)

    def handle_login(self):
        data = self.read_json()
        if data is None:
            return
        username = str(data.get("username") or "").strip()
        password = str(data.get("password") or "")
        with db() as conn:
            user = conn.execute(
                "SELECT id, username, display_name, password_hash FROM users WHERE username = ?",
                (username,),
            ).fetchone()
            if not user or not check_password(password, user["password_hash"]):
                self.send_error_json(401, "用户名或密码不正确")
                return
            token = issue_session(conn, user["id"])
        self.send_json({"user": public_user(user)}, cookie=token)

    def handle_logout(self):
        if self.read_json() is None and int(self.headers.get("Content-Length") or 0):
            return
        with db() as conn:
            clear_session(conn, self)
        self.send_json({"ok": True}, clear_cookie=True)

    def handle_me(self):
        with db() as conn:
            user = current_user(conn, self)
        self.send_json({"user": public_user(user) if user else None})

    def handle_get_state(self):
        with db() as conn:
            user = current_user(conn, self)
            if not user:
                self.send_error_json(401, "请先登录")
                return
            row = conn.execute(
                "SELECT state_json, updated_at FROM user_states WHERE user_id = ?",
                (user["id"],),
            ).fetchone()
        if not row:
            self.send_json({"saved": False, "state": None})
            return
        try:
            state = json.loads(row["state_json"])
        except json.JSONDecodeError:
            state = None
        self.send_json({"saved": True, "updatedAt": row["updated_at"], "state": state})

    def handle_put_state(self):
        data = self.read_json()
        if data is None:
            return
        with db() as conn:
            user = current_user(conn, self)
            if not user:
                self.send_error_json(401, "请先登录")
                return
            payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
            conn.execute(
                """
                INSERT INTO user_states (user_id, state_json, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET state_json = excluded.state_json, updated_at = excluded.updated_at
                """,
                (user["id"], payload, now_iso()),
            )
            conn.commit()
        self.send_json({"ok": True})

    def handle_leaderboard(self, query):
        sort = (query.get("sort") or ["totalReturn"])[0]
        if sort not in SORT_KEYS:
            sort = "totalReturn"
        try:
            limit = max(1, min(100, int((query.get("limit") or ["100"])[0])))
        except ValueError:
            limit = 100
        with db() as conn:
            user = current_user(conn, self)
            rows = conn.execute("SELECT id, user_id, snapshot_json, updated_at FROM strategies").fetchall()
        items = [strategy_view(row) for row in rows]
        reverse = sort != "maxDrawdown"
        items.sort(key=lambda item: metric_number(item.get(sort)), reverse=reverse)
        self.send_json(
            {
                "member": user is not None,
                "membership": {"active": user is not None, "tier": "member" if user else "guest"},
                "strategies": items[:limit],
            }
        )

    def handle_strategy_detail(self, strategy_id):
        with db() as conn:
            user = current_user(conn, self)
            row = conn.execute(
                "SELECT id, user_id, snapshot_json, updated_at FROM strategies WHERE id = ?",
                (strategy_id,),
            ).fetchone()
        if not row:
            self.send_error_json(404, "没有找到这个策略")
            return
        self.send_json(strategy_view(row, include_config=user is not None))

    def handle_snapshot(self):
        data = self.read_json()
        if data is None:
            return
        with db() as conn:
            user = current_user(conn, self)
            if not user:
                self.send_error_json(401, "请先登录")
                return
            payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
            conn.execute(
                """
                INSERT INTO strategies (user_id, snapshot_json, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET snapshot_json = excluded.snapshot_json, updated_at = excluded.updated_at
                """,
                (user["id"], payload, now_iso()),
            )
            conn.commit()
            row = conn.execute("SELECT id FROM strategies WHERE user_id = ?", (user["id"],)).fetchone()
        self.send_json({"ok": True, "id": str(row["id"])})

    def handle_hot_push(self):
        data = self.read_json()
        if data is None:
            return
        with db() as conn:
            user = current_user(conn, self)
            if not user:
                self.send_error_json(401, "请先登录")
                return
        if not HOT_PUSH_URL:
            self.send_error_json(503, "热币推送地址未配置")
            return
        symbols = select_hot_symbols(data)
        if symbols is None:
            self.send_error_json(400, "请提供 coins、symbol 或 symbols")
            return
        result = queue_hot_symbols(symbols)
        self.send_json({"ok": True, "config": hot_push_config(), **result})


def main():
    init_db()
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    shown = "127.0.0.1" if HOST in {"0.0.0.0", "::"} else HOST
    print(f"Strong Coin Radar http://{shown}:{PORT}/")
    print("A domain pointed at this host opens the app at /. Set PORT=80 to omit the port.")
    print(f"Database {DB_PATH}")
    cfg = hot_push_config()
    print(
        f"Hot push {cfg['url'] or '(disabled)'} · cooldown {cfg['cooldownHours']}h · "
        f"score>={cfg['minScore']} change>={cfg['minChange']}% rvol>={cfg['minRvol']}x vol>={cfg['minVolumeM']}M"
    )
    httpd.serve_forever()


if __name__ == "__main__":
    main()
