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
            """
        )


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


def public_host(handler):
    forwarded = handler.headers.get("X-Forwarded-Host")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return handler.headers.get("Host") or ""


def same_origin(handler):
    origin = handler.headers.get("Origin")
    if not origin:
        return True
    host = public_host(handler)
    return origin in {f"http://{host}", f"https://{host}"}


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


def main():
    init_db()
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    shown = "127.0.0.1" if HOST in {"0.0.0.0", "::"} else HOST
    print(f"Strong Coin Radar http://{shown}:{PORT}/")
    print("A domain pointed at this host opens the app at /. Set PORT=80 to omit the port.")
    print(f"Database {DB_PATH}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
