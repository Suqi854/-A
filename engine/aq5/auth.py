"""登录：个人单用户。密码用 scrypt 加盐哈希；登录后发 30 天会话 Cookie。

- 首次使用在本机（127.0.0.1）打开页面创建账号；局域网设备（手机）必须登录。
- “本机免登录”默认开启：在这台电脑上直接用，不打扰你。
- API Token：给插件 / 脚本调用用（请求头 Authorization: Bearer <token>）。
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from . import db
from .config import now

SCHEMA = """
CREATE TABLE IF NOT EXISTS auth_user(id INTEGER PRIMARY KEY CHECK(id=1), username TEXT, pw TEXT, salt TEXT,
  created TEXT, profile TEXT);
CREATE TABLE IF NOT EXISTS auth_session(token TEXT PRIMARY KEY, created REAL, expires REAL, ip TEXT, ua TEXT,
  kind TEXT DEFAULT 'web', name TEXT);
"""
SESSION_DAYS = 30
_fails: dict[str, list[float]] = {}
LOCAL = ("127.0.0.1", "::1")


def init() -> None:
    with db.tx() as c:
        c.executescript(SCHEMA)


def _hash(pw: str, salt: str) -> str:
    return hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1, dklen=32).hex()


def has_user() -> bool:
    return bool(db.q1("SELECT 1 FROM auth_user WHERE id=1"))


def user() -> dict | None:
    u = db.q1("SELECT username, created, profile FROM auth_user WHERE id=1")
    if u:
        u["profile"] = db.loads(u["profile"], {})
    return u


def setup(username: str, password: str) -> None:
    if has_user():
        raise ValueError("账号已经存在")
    _check_pw(password)
    salt = secrets.token_hex(16)
    with db.tx() as c:
        c.execute("INSERT INTO auth_user VALUES(1,?,?,?,?,?)",
                  (username.strip() or "me", _hash(password, salt), salt, now().strftime("%Y-%m-%d %H:%M"), "{}"))
    db.event("system", "已创建登录账号", username)


def _check_pw(pw: str) -> None:
    if len(pw) < 8 or pw.isdigit() or pw.isalpha():
        raise ValueError("密码至少 8 位，并且要同时包含字母和数字")


def login(username: str, password: str, ip: str, ua: str) -> str:
    t = time.time()
    fails = [x for x in _fails.get(ip, []) if t - x < 600]
    if len(fails) >= 5:
        raise ValueError("失败次数太多，请 10 分钟后再试")
    u = db.q1("SELECT username, pw, salt FROM auth_user WHERE id=1")
    if not u or u["username"] != username.strip() or not hmac.compare_digest(u["pw"], _hash(password, u["salt"])):
        _fails[ip] = fails + [t]
        raise ValueError("用户名或密码错误")
    _fails.pop(ip, None)
    return _new_session(ip, ua, "web", "网页登录", SESSION_DAYS * 86400)


def _new_session(ip, ua, kind, name, ttl) -> str:
    tok = secrets.token_urlsafe(32)
    with db.tx() as c:
        c.execute("INSERT INTO auth_session VALUES(?,?,?,?,?,?,?)",
                  (hashlib.sha256(tok.encode()).hexdigest(), time.time(), time.time() + ttl, ip, ua[:120], kind, name))
    return tok


def check(token: str | None) -> dict | None:
    if not token:
        return None
    s = db.q1("SELECT * FROM auth_session WHERE token=?", (hashlib.sha256(token.encode()).hexdigest(),))
    if not s or s["expires"] < time.time():
        return None
    return s


def logout(token: str | None) -> None:
    if token:
        with db.tx() as c:
            c.execute("DELETE FROM auth_session WHERE token=?", (hashlib.sha256(token.encode()).hexdigest(),))


def change_password(old: str, new: str) -> None:
    u = db.q1("SELECT pw, salt FROM auth_user WHERE id=1")
    if not u or not hmac.compare_digest(u["pw"], _hash(old, u["salt"])):
        raise ValueError("原密码不正确")
    _check_pw(new)
    salt = secrets.token_hex(16)
    with db.tx() as c:
        c.execute("UPDATE auth_user SET pw=?, salt=? WHERE id=1", (_hash(new, salt), salt))
        c.execute("DELETE FROM auth_session WHERE kind='web'")  # 改密码后所有设备重新登录


def sessions() -> list[dict]:
    rows = db.q("SELECT token, created, expires, ip, ua, kind, name FROM auth_session WHERE expires>? "
                "ORDER BY created DESC", (time.time(),))
    for r in rows:
        r["id"] = r.pop("token")[:12]
        r["created"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["created"]))
        r["expires"] = time.strftime("%Y-%m-%d", time.localtime(r["expires"]))
    return rows


def revoke(prefix: str) -> None:
    with db.tx() as c:
        c.execute("DELETE FROM auth_session WHERE substr(token,1,12)=?", (prefix,))


def create_api_token(name: str, days: int = 365) -> str:
    return _new_session("-", "api", "api", name or "API", days * 86400)


def save_profile(p: dict) -> dict:
    allowed = {"nickname", "risk", "capital", "goal", "notes", "style"}
    cur = (user() or {}).get("profile", {})
    cur.update({k: v for k, v in p.items() if k in allowed})
    with db.tx() as c:
        c.execute("UPDATE auth_user SET profile=? WHERE id=1", (db.dumps(cur),))
    return cur
