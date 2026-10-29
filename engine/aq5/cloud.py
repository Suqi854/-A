"""云端代跑：电脑关机时由 GitHub Actions 接着跑（自动选股、AI 自动盘、自选信号、发布网页），电脑开机后无缝接回。

状态交接（都在 GitHub 仓库的 state 分支，只有一次提交，强制覆盖）：
  cloud.json  明文元数据：谁在跑（pc / cloud）、是否在运行、心跳时间、状态版本时间
  state.bin   加密的核心数据库（选股、模拟盘、学习样本、迭代权重、信号、AI 笔记…，不含行情缓存和账号会话）
              + settings.json；用「手机查看密码」加密（云端用 GitHub Secret AQ5_KEY 解密）

规则：
  - 交易日 08:30-16:00 云端全程值守：电脑在线（固定地址 /__aq5/alive 能连到电脑）时只待命不交易；
    电脑连续 3 分钟连不上才接手；接手后电脑上线，存好状态交还；单次运行快到 6 小时上限时自己接力下一次。
  - 电脑启动时：若云端正在跑，先等它交接（最多约 5 分钟）；若云端状态比本机新，导入云端状态再开始。
  - 电脑交易时段每 5 分钟、收盘后上传一次自己的状态，保证云端接手时是最新的。
"""
from __future__ import annotations

import base64
import gzip
import io
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import db, vault
from .config import DATA, DB_PATH, SETTINGS_PATH, load_settings, now, save_settings

CLOUD = os.environ.get("AQ5_CLOUD") == "1"
BRANCH = "state"
EXCLUDE = {"klines", "kline_meta", "fundflow", "news", "auth_user", "auth_session", "sqlite_sequence"}
STATE = {"role": "cloud" if CLOUD else "pc", "last_up": None, "last_pull": None, "error": None, "waiting": False,
         "remote": None}
_lock = threading.Lock()


def _cfg() -> dict:
    s = load_settings()
    c = s.get("cloud", {})
    pub = s.get("publish", {})
    repo = os.environ.get("GITHUB_REPOSITORY") if CLOUD else (c.get("repo") or _repo_of(pub.get("remote", "")))
    remote = f"https://github.com/{repo}.git" if CLOUD and repo else pub.get("remote") or ""
    relay = os.environ.get("AQ5_RELAY") or (s.get("remote", {}) or {}).get("relay_url") or c.get("relay") or ""
    return {"enabled": c.get("enabled", True), "remote": remote, "repo": repo or "", "relay": relay,
            "last_sync": c.get("last_sync") or ""}


def _repo_of(url: str) -> str:
    u = url.strip().rstrip("/")
    if u.endswith(".git"):
        u = u[:-4]
    parts = u.split("github.com/")
    return parts[1] if len(parts) > 1 else ""


def _key() -> str:
    return os.environ.get("AQ5_KEY") or vault.get("mobile_password")


def _http(url: str, headers: dict | None = None, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "aq5-cloud", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# ----------------------------------------------------------------------------- 电脑是否在线
def pc_alive() -> bool:
    rel = _cfg()["relay"]
    if not rel:
        return False
    try:
        return bool(json.loads(_http(rel.rstrip("/") + "/__aq5/alive?t=" + str(int(time.time())), timeout=15)).get("alive"))
    except Exception:  # noqa: BLE001
        return False


# ----------------------------------------------------------------------------- 读远端 state 分支
def _gh_raw(path: str) -> bytes | None:
    repo = _cfg()["repo"]
    if not repo:
        return None
    hdr = {"Accept": "application/vnd.github.raw"}
    tok = os.environ.get("GITHUB_TOKEN")
    if tok:
        hdr["Authorization"] = "Bearer " + tok
    try:
        return _http(f"https://api.github.com/repos/{repo}/contents/{path}?ref={BRANCH}&t={int(time.time())}", hdr, timeout=60)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def remote_meta() -> dict | None:
    b = _gh_raw("cloud.json")
    return json.loads(b) if b else None


def _age(ts: str | None) -> float:
    if not ts:
        return 1e9
    try:
        from datetime import datetime
        t = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=now().tzinfo)
        return (now() - t).total_seconds()
    except ValueError:
        return 1e9


# ----------------------------------------------------------------------------- 打包 / 导入
def _tables(c: sqlite3.Connection) -> list[str]:
    return [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'") if r[0] not in EXCLUDE]


def pack() -> bytes:
    key = _key()
    if not key:
        raise ValueError("没有设置「手机查看密码」，无法加密云端状态")
    tmp = Path(tempfile.mkdtemp()) / "s.db"
    src = sqlite3.connect(DB_PATH, timeout=30)
    dst = sqlite3.connect(tmp)
    try:
        src.backup(dst)
    finally:
        src.close()
    try:
        for t in [r[0] for r in dst.execute("SELECT name FROM sqlite_master WHERE type='table'")]:
            if t in EXCLUDE and t != "sqlite_sequence":
                dst.execute(f"DELETE FROM {t}")
        dst.commit()
        dst.execute("VACUUM")
        dst.close()
        blob = {"db": base64.b64encode(gzip.compress(tmp.read_bytes(), 6)).decode(),
                "settings": json.loads(SETTINGS_PATH.read_text("utf-8")) if SETTINGS_PATH.exists() else {}}
    finally:
        shutil.rmtree(tmp.parent, ignore_errors=True)
    return vault.seal(json.dumps(blob).encode(), key)


def unpack(blob: bytes, take_settings: bool) -> dict:
    key = _key()
    if not key:
        raise ValueError("缺少解密口令（AQ5_KEY / 手机查看密码）")
    d = json.loads(vault.unseal(blob, key))
    tmp = Path(tempfile.mkdtemp()) / "s.db"
    tmp.write_bytes(gzip.decompress(base64.b64decode(d["db"])))
    n = 0
    try:
        src = sqlite3.connect(tmp)
        tabs = _tables(src)
        src.close()
        with db.tx() as c:
            c.execute("ATTACH DATABASE ? AS cs", (str(tmp),))
            mine = {r[0] for r in c.execute("SELECT name FROM main.sqlite_master WHERE type='table'")}
            for t in tabs:
                if t not in mine:
                    continue
                cols_src = {r[1] for r in c.execute(f"PRAGMA cs.table_info({t})")}
                cols = [r[1] for r in c.execute(f"PRAGMA main.table_info({t})") if r[1] in cols_src]
                cl = ",".join(f'"{x}"' for x in cols)
                c.execute(f"DELETE FROM main.{t}")
                c.execute(f"INSERT INTO main.{t}({cl}) SELECT {cl} FROM cs.{t}")
                n += 1
        db.conn().execute("DETACH DATABASE cs")
    finally:
        shutil.rmtree(tmp.parent, ignore_errors=True)
    if take_settings and d.get("settings"):
        SETTINGS_PATH.write_text(json.dumps(d["settings"], ensure_ascii=False, indent=2), "utf-8")
    return {"tables": n}


# ----------------------------------------------------------------------------- 上传（git 强推单提交）
def _git(args, cwd, timeout=120):
    from .publisher import _git as g
    return g(args, cwd, timeout)


def upload(active: bool, final: bool = False) -> dict:
    cfg = _cfg()
    if not cfg["remote"] or not _key():
        return {"ok": False, "error": "未配置发布仓库或手机查看密码"}
    with _lock:
        root = DATA / "state_repo"
        root.mkdir(exist_ok=True)
        if not (root / ".git").exists():
            _git(["init", "-q"], root)
            _git(["checkout", "-q", "-b", BRANCH], root)
            _git(["config", "user.name", "aq5-state"], root)
            _git(["config", "user.email", "aq5-state@users.noreply.github.com"], root)
        rc, remotes = _git(["remote"], root)
        _git(["remote", "add" if "origin" not in remotes.split() else "set-url", "origin", cfg["remote"]], root)
        ts = now().strftime("%Y-%m-%d %H:%M:%S")
        (root / "state.bin").write_bytes(pack())
        meta = {"owner": STATE["role"], "active": bool(active), "heartbeat": ts, "updated": ts,
                "note": "A量化云端交接状态（state.bin 已加密）。请勿手工修改。"}
        (root / "cloud.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), "utf-8")
        _git(["add", "-A"], root)
        has_head = _git(["rev-parse", "--verify", "HEAD"], root)[0] == 0
        _git(["commit", "-q", "--amend", "-m", f"state {ts}"] if has_head else ["commit", "-q", "-m", f"state {ts}"], root)
        code, out = _git(["push", "-q", "--force", "origin", f"HEAD:{BRANCH}"], root, timeout=180)
        if code:
            STATE["error"] = out[-300:]
            return {"ok": False, "error": out[-300:]}
        STATE["last_up"] = ts
        STATE["error"] = None
        if not CLOUD:
            save_settings({"cloud": {"last_sync": ts}})
        return {"ok": True, "time": ts}


# ----------------------------------------------------------------------------- 电脑端：启动交接 + 定时上传
def reconcile(max_wait: int = 330) -> dict:
    """电脑启动时调用（在调度器、高频引擎启动之前）。"""
    cfg = _cfg()
    if CLOUD or not cfg["enabled"] or not cfg["repo"] or not _key():
        return {"skipped": True}
    try:
        meta = remote_meta()
        STATE["remote"] = meta
        t0 = time.time()
        while meta and meta.get("owner") == "cloud" and meta.get("active") and _age(meta.get("heartbeat")) < 420 \
                and time.time() - t0 < max_wait:
            STATE["waiting"] = True
            db.event("system", "云端正在代跑，等它交接…", "最多约 5 分钟", "info")
            time.sleep(20)
            meta = remote_meta()
            STATE["remote"] = meta
        STATE["waiting"] = False
        res = {"pulled": False}
        if meta and meta.get("owner") == "cloud" and (meta.get("updated") or "") > (cfg["last_sync"] or ""):
            blob = _gh_raw("state.bin")
            if blob:
                local = DATA / "backups"
                local.mkdir(exist_ok=True)
                from .backup import db_snapshot
                try:
                    db_snapshot()
                except Exception:  # noqa: BLE001
                    pass
                r = unpack(blob, take_settings=False)
                save_settings({"cloud": {"last_sync": meta.get("updated")}})
                STATE["last_pull"] = now().strftime("%Y-%m-%d %H:%M:%S")
                db.event("system", "已接回云端代跑的数据", f"云端最后状态 {meta.get('updated')}，导入 {r['tables']} 张表", "good")
                res = {"pulled": True, **r}
        upload(active=False)
        return res
    except Exception as e:  # noqa: BLE001
        STATE["error"] = str(e)[:200]
        STATE["waiting"] = False
        db.event("system", "云端交接失败", str(e)[:300], "warn")
        return {"error": str(e)}


def uploader() -> None:
    """电脑端：交易时段每 5 分钟、收盘后 15:58 上传一次状态。"""
    def loop():
        from . import market
        last, done_close = 0.0, ""
        while True:
            try:
                if _cfg()["enabled"] and market.is_trading_day():
                    hm = now().hour * 100 + now().minute
                    if 915 <= hm <= 1505 and time.time() - last > 300:
                        last = time.time()
                        upload(active=False)
                    if hm >= 1558 and done_close != now().strftime("%Y-%m-%d"):
                        done_close = now().strftime("%Y-%m-%d")
                        upload(active=False)
            except Exception as e:  # noqa: BLE001
                STATE["error"] = str(e)[:200]
            time.sleep(30)
    if not CLOUD:
        threading.Thread(target=loop, daemon=True, name="aq5-cloudup").start()


def status() -> dict:
    return {**STATE, "enabled": _cfg()["enabled"], "repo": _cfg()["repo"], "key": bool(_key())}


# ----------------------------------------------------------------------------- 云端：交易日值守
WORKFLOW = "aq5-cloud.yml"
OFFLINE_CONFIRM = 3      # 连续 3 次（约 3 分钟）连不上电脑才接手：隧道偶尔抖一下不会两边同时交易
GUARD_START, GUARD_END = 830, 1600


def dispatch_next(reason: str) -> bool:
    """接力：用 Actions 自带的 GITHUB_TOKEN 触发下一次云端值守（workflow_dispatch 允许自触发）。"""
    repo, tok = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_TOKEN")
    if not (repo and tok):
        return False
    body = json.dumps({"ref": os.environ.get("GITHUB_REF_NAME") or "main", "inputs": {"mode": "run"}}).encode()
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/actions/workflows/{WORKFLOW}/dispatches", data=body,
                                 method="POST", headers={"Authorization": "Bearer " + tok, "Accept": "application/vnd.github+json",
                                                         "User-Agent": "aq5-cloud", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            ok = r.status in (200, 204)
    except Exception as e:  # noqa: BLE001
        print("接力失败：", e, flush=True)
        return False
    print(f"已接力下一次云端值守（{reason}）", flush=True)
    return ok


def _guard_hours() -> bool:
    hm = now().hour * 100 + now().minute
    return now().weekday() < 5 and hm < GUARD_END


def run_cloud(max_minutes: int = 340) -> int:
    """GitHub Actions 里执行：python -m aq5.cloud

    交易日 08:30-16:00 值守：电脑在线就待命（每分钟看一次）；电脑确认离线（连续 3 分钟）就导入最新状态接手交易；
    接手后电脑回来，存好状态交还并接力一个新的值守任务；单次快到 6 小时上限时也接力下一次，保证全天有人守着。
    """
    from . import ai, hf, market, marketstate, picker, scheduler, sectors, signals, sim  # noqa: F401
    print("== A量化 云端值守 ==", now().strftime("%Y-%m-%d %H:%M:%S"), flush=True)
    if not _guard_hours():
        print("不在交易日值守时段（08:30-16:00），退出", flush=True)
        return 0
    if not remote_meta():
        print("还没有电脑上传的状态（state 分支为空），退出", flush=True)
        return 0
    db.init()
    from . import announce, auth
    for m in (auth, ai, sectors, signals, marketstate, announce):
        try:
            m.init()
        except Exception as e:  # noqa: BLE001
            print("init", m.__name__, e, flush=True)
    while now().hour * 100 + now().minute < GUARD_START:  # 提前被触发：先等到 08:30
        time.sleep(60)
    try:
        if not market.is_trading_day():
            print("今天休市，退出", flush=True)
            return 0
    except Exception as e:  # noqa: BLE001
        print("交易日判断失败，按交易日处理：", e, flush=True)
    t0, last_up, misses, active, reason = time.time(), 0.0, 0, False, "到时"
    while True:
        hm = now().hour * 100 + now().minute
        if hm >= GUARD_END:
            reason = "收盘后结束"
            break
        if time.time() - t0 > max_minutes * 60:
            reason = "单次运行接近 6 小时上限"
            dispatch_next(reason)
            break
        alive = pc_alive()
        misses = 0 if alive else misses + 1
        if not active:
            if misses >= OFFLINE_CONFIRM:
                _take_over(sim, hf, scheduler)
                active, last_up = True, time.time()
            elif misses == 0 and int(time.time() - t0) % 1800 < 60:
                print(now().strftime("%H:%M"), "电脑在线，云端待命", flush=True)
        else:
            if alive:
                reason = "电脑上线，交还给电脑"
                dispatch_next("交还后继续值守")
                break
            if time.time() - last_up > 300:
                last_up = time.time()
                res = upload(active=True)
                print(now().strftime("%H:%M"), "状态已上传" if res.get("ok") else res, flush=True)
        time.sleep(60)
    if active:
        db.event("system", f"云端代跑结束（{reason}）", "", "info")
        res = upload(active=False, final=True)
        print("结束：", reason, res, flush=True)
    else:
        print("值守结束：", reason, "（期间电脑一直在线，云端没有交易）", flush=True)
    return 0


def _take_over(sim, hf, scheduler) -> None:
    """电脑确认离线：导入电脑最后上传的状态，开始交易和发布。"""
    blob = _gh_raw("state.bin")
    meta = remote_meta() or {}
    r = unpack(blob, take_settings=True)
    print(now().strftime("%H:%M"), f"电脑离线，云端接手。导入状态：{meta.get('owner')} {meta.get('updated')}，{r['tables']} 张表", flush=True)
    save_settings({"remote": {"enabled": False}, "backup": {"enabled": False}})  # 云端：不开隧道、不推私有备份；发布网页照常
    sec = {"mobile_password": os.environ.get("AQ5_KEY", "")}
    if os.environ.get("AQ5_AI_KEY"):
        sec["ai_key"] = os.environ["AQ5_AI_KEY"]
    vault.set_many(sec)
    sim.ensure_accounts()
    db.event("system", "云端代跑开始", "电脑离线超过 3 分钟，GitHub 云端接手：自动选股、AI 自动盘、计划模拟、自选信号、发布网页", "info")
    upload(active=True)
    hf.start()
    scheduler.start()


def diag() -> int:
    """在 GitHub 云端测试各行情源能否访问（不导入状态、不交易）。"""
    from . import market
    db.init()
    print("电脑在线：", pc_alive(), "  relay:", _cfg()["relay"], flush=True)
    meta = remote_meta()
    print("state 分支：", meta, flush=True)
    if os.environ.get("AQ5_KEY"):
        try:
            blob = _gh_raw("state.bin")
            d = json.loads(vault.unseal(blob, os.environ["AQ5_KEY"])) if blob else None
            print("AQ5_KEY：", "正确，能解开电脑上传的状态" if d else "state.bin 还不存在", flush=True)
        except Exception as e:  # noqa: BLE001
            print("AQ5_KEY：错误 ——", e, "（请确认填的是电脑上设置的「手机查看密码」）", flush=True)
    else:
        print("AQ5_KEY：未设置", flush=True)
    print("AQ5_AI_KEY：", "已设置" if os.environ.get("AQ5_AI_KEY") else "未设置", flush=True)
    if os.environ.get("AQ5_AI_KEY") and meta:
        try:
            st = json.loads(vault.unseal(_gh_raw("state.bin"), os.environ["AQ5_KEY"]))["settings"]
            from . import config
            config.SETTINGS_PATH.write_text(json.dumps(st, ensure_ascii=False), "utf-8")
            vault.set_many({"ai_key": os.environ["AQ5_AI_KEY"]})
            from . import ai
            ai.init()
            r = ai.chat([{"role": "user", "content": "回复两个字：可用"}], max_tokens=10) if hasattr(ai, "chat") else None
            print("AI 接口：", "可用 ——" if r else "未测试", (r[0] or "")[:40] if r else "", flush=True)
        except Exception as e:  # noqa: BLE001
            print("AI 接口：测试失败 ——", str(e)[:200], flush=True)
    tests = [("腾讯实时 quotes", lambda: len(market.quotes(["sh600519", "sh000001"]))),
             ("腾讯高频 quotes_fast", lambda: len(market.quotes_fast(["sh600519"]))),
             ("日K kline", lambda: len(market.kline("sh600519", 60))),
             ("周K", lambda: len(market.kline_period("sh600519", "week", 30).get("rows") or [])),
             ("5分钟K", lambda: len(market.kline_period("sh600519", "m5", 48).get("rows") or [])),
             ("分时 intraday", lambda: len(market.intraday("sh600519").get("points") or [])),
             ("主力资金 fundflow", lambda: len(market.fundflow("sh600519") or [])),
             ("全市场快照 snapshot", lambda: len(market.snapshot(force=True)))]
    ok = 0
    for name, fn in tests:
        t = time.time()
        try:
            n = fn()
            ok += 1
            print(f"  OK  {name}: {n} 条  {time.time() - t:.1f}s", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"  ERR {name}: {type(e).__name__} {str(e)[:160]}", flush=True)
    try:
        print("数据源统计：", json.dumps(market.SRC_STATS, ensure_ascii=False)[:1500], flush=True)
    except Exception:  # noqa: BLE001
        pass
    print(f"== {ok}/{len(tests)} 通过 ==", flush=True)
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(diag() if sys.argv[1:] == ["diag"] else run_cloud())
