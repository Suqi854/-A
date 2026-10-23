"""个人数据加密备份：我的模拟盘、成交、自选、个人资料、设置 → 加密成一个文件 → 推送到你的 GitHub 私有仓库。

仓库里只有密文（aq5-personal.enc），没有备份密码谁也解不开；备份密码存在本机保险箱里，
换电脑恢复时需要你自己输入。密钥（API Key / token）不在备份里。
"""
from __future__ import annotations

import gzip
import json
import shutil
import subprocess
import threading

from . import auth, db, vault
from .config import DATA, load_settings, now, save_settings

REPO = DATA / "private_backup"
FILE = "aq5-personal.enc"
STATE = {"last": None, "error": None, "running": False}
_lock = threading.Lock()
TABLES = ("accounts", "positions", "orders", "trades", "equity")


def export_personal() -> dict:
    data = {t: db.q(f"SELECT * FROM {t} WHERE {'id' if t == 'accounts' else 'account'}='me'") for t in TABLES}
    s = load_settings()
    s.pop("notify", None)
    u = auth.user() or {}
    return {"version": 1, "time": now().strftime("%Y-%m-%d %H:%M:%S"), "tables": data, "settings": s,
            "profile": u.get("profile", {}), "username": u.get("username")}


def import_personal(d: dict) -> dict:
    with db.tx() as c:
        idmap = {}
        for t in TABLES:
            c.execute(f"DELETE FROM {t} WHERE {'id' if t == 'accounts' else 'account'}='me'")
            for r in d["tables"].get(t, []):
                r = dict(r)
                old = r.pop("id") if t in ("orders", "trades") else None
                if t == "trades" and r.get("order_id") in idmap:
                    r["order_id"] = idmap[r["order_id"]]
                cols = ",".join(r)
                cur = c.execute(f"INSERT INTO {t}({cols}) VALUES({','.join('?' * len(r))})", tuple(r.values()))
                if t == "orders":
                    idmap[old] = cur.lastrowid
    s = d.get("settings") or {}
    s.pop("publish", None)
    save_settings({k: v for k, v in s.items() if k in ("watchlist", "pick", "risk", "indices", "overseas", "init_cash")})
    if d.get("profile") and auth.has_user():
        auth.save_profile(d["profile"])
    db.event("system", "已从加密备份恢复个人数据", d.get("time", ""))
    return {"restored": {t: len(d["tables"].get(t, [])) for t in TABLES}, "time": d.get("time")}


def _git(args, timeout=90):
    try:
        p = subprocess.run(["git", *args], cwd=str(REPO), capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
        return p.returncode, (p.stdout + p.stderr).strip()
    except FileNotFoundError:
        return 127, "没有找到 git"
    except subprocess.TimeoutExpired:
        return 124, "git 超时"


def _ensure_repo(remote: str) -> None:
    REPO.mkdir(parents=True, exist_ok=True)
    if not (REPO / ".git").exists():
        code, out = _git(["clone", "-q", remote, "."], timeout=120)
        if code:  # 空仓库或克隆失败：本地初始化
            shutil.rmtree(REPO / ".git", ignore_errors=True)
            _git(["init", "-q"])
            _git(["checkout", "-q", "-b", "main"])
            _git(["remote", "add", "origin", remote])
        _git(["config", "user.name", "aq5-backup"])
        _git(["config", "user.email", "aq5-backup@users.noreply.github.com"])
    else:
        _git(["remote", "set-url", "origin", remote])


def backup(push: bool = True) -> dict:
    cfg = load_settings().get("backup", {})
    pw = vault.get("backup_password")
    if not pw:
        raise ValueError("先在「个人设置」里设置备份密码")
    if push and not cfg.get("remote"):
        raise ValueError("先填写私有仓库地址")
    with _lock:
        if STATE["running"]:
            return {"skipped": "正在备份"}
        STATE["running"] = True
    try:
        blob = vault.seal(gzip.compress(json.dumps(export_personal(), ensure_ascii=False).encode()), pw)
        (DATA / "backups").mkdir(exist_ok=True)
        local = DATA / "backups" / f"aq5-personal-{now().strftime('%Y%m%d')}.enc"
        local.write_bytes(blob)  # 本机也留一份（按天覆盖）
        res = {"local": str(local), "bytes": len(blob)}
        if push:
            _ensure_repo(cfg["remote"])
            (REPO / FILE).write_bytes(blob)
            (REPO / "README.md").write_text("# A量化 个人数据（加密）\n\n`aq5-personal.enc` 为加密文件，需要备份密码才能在 A量化「个人设置 → 恢复」中导入。\n"
                                            "请保持本仓库为 **Private**。\n", "utf-8")
            _git(["add", "-A"])
            _git(["commit", "-q", "-m", f"backup {now().strftime('%Y-%m-%d %H:%M')}"])
            code, out = _git(["push", "-q", "-u", "origin", "HEAD:main"], timeout=120)
            if code:
                raise RuntimeError("推送失败：" + out[-300:])
            res["pushed"] = True
        STATE.update(last=now().strftime("%Y-%m-%d %H:%M:%S"), error=None)
        db.event("system", "个人数据已加密备份", "已推送到私有仓库" if push else "仅本机")
        return res
    except Exception as e:
        STATE["error"] = str(e)[:300]
        raise
    finally:
        STATE["running"] = False


def restore(password: str | None = None) -> dict:
    cfg = load_settings().get("backup", {})
    pw = password or vault.get("backup_password")
    if not pw:
        raise ValueError("请输入备份密码")
    if cfg.get("remote"):
        _ensure_repo(cfg["remote"])
        _git(["pull", "-q", "origin", "main"], timeout=120)
    f = REPO / FILE
    if not f.exists():
        loc = sorted((DATA / "backups").glob("aq5-personal-*.enc")) if (DATA / "backups").exists() else []
        if not loc:
            raise ValueError("没有找到备份文件")
        f = loc[-1]
    d = json.loads(gzip.decompress(vault.unseal(f.read_bytes(), pw)))
    if password:
        vault.set_many({"backup_password": password})
    return import_personal(d)
