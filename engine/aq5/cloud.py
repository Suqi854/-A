"""云端代跑：电脑关机时由 GitHub Actions 接着跑（自动选股、AI 自动盘、自选信号、发布网页），电脑开机后无缝接回。

状态交接（都在 GitHub 仓库的 state 分支，只有一次提交，强制覆盖）：
  cloud.json  明文元数据：谁在跑（pc / cloud）、是否在运行、心跳时间、状态版本时间
  state.manifest.json / state-part-*.bin：同一加密状态的有校验分片（兼容读取旧 state.bin）
              核心数据库（选股、模拟盘、学习样本、迭代权重、信号、AI 笔记…，不含行情缓存和账号会话）
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
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import db, vault
from .config import DATA, DB_PATH, SETTINGS_PATH, load_settings, now, save_settings

CLOUD = os.environ.get("AQ5_CLOUD") == "1"
BRANCH = "state"
STATE_MANIFEST = "state.manifest.json"
STATE_CHUNK_BYTES = 16 * 1024 * 1024
EXCLUDE = {"intraday_samples", "klines", "kline_meta", "fundflow", "news", "auth_user", "auth_session", "sqlite_sequence"}
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
def _gh_raw(path: str, ref: str | None = None) -> bytes | None:
    repo = _cfg()["repo"]
    if not repo:
        return None
    hdr = {"Accept": "application/vnd.github.raw"}
    tok = os.environ.get("GITHUB_TOKEN")
    if tok:
        hdr["Authorization"] = "Bearer " + tok
    try:
        ref = urllib.parse.quote(ref or BRANCH, safe="")
        return _http(f"https://api.github.com/repos/{repo}/contents/{path}?ref={ref}&t={int(time.time())}", hdr, timeout=60)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def _state_ref() -> str | None:
    """Resolve the mutable branch once; every file is then read at this commit."""
    repo = _cfg()["repo"]
    if not repo:
        return None
    headers = {"Accept": "application/vnd.github+json"}
    if os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["GITHUB_TOKEN"]
    try:
        result = json.loads(_http(f"https://api.github.com/repos/{repo}/git/ref/heads/{BRANCH}?t={int(time.time())}", headers, timeout=60))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    commit = (result.get("object") or {}).get("sha")
    if not isinstance(commit, str) or not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise ValueError("state_commit_invalid")
    return commit


def remote_meta() -> dict | None:
    commit = _state_ref()
    if not commit:
        return None
    b = _gh_raw("cloud.json", ref=commit)
    if not b:
        return None
    meta = json.loads(b)
    if not isinstance(meta, dict):
        raise ValueError("state_metadata_invalid")
    return {**meta, "_state_commit": commit}


def read_remote_state(ref: str | None = None) -> bytes | None:
    """Reassemble immutable encrypted bytes; authenticate only after all checks.

    Legacy state.bin is also pinned to the same resolved commit. No live branch
    fallback is permitted when a manifest exists but is incomplete or invalid.
    """
    commit = ref or _state_ref()
    if not commit:
        return None
    if not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise ValueError("state_commit_invalid")
    raw = _gh_raw(STATE_MANIFEST, ref=commit)
    if raw is None:
        return _gh_raw("state.bin", ref=commit)
    if len(raw) > 1024 * 1024:
        raise ValueError("state_manifest_too_large")
    manifest = json.loads(raw)
    chunks = manifest.get("chunks") if isinstance(manifest, dict) else None
    sha = manifest.get("sha256") if isinstance(manifest, dict) else None
    total = manifest.get("size") if isinstance(manifest, dict) else None
    if (not isinstance(manifest, dict) or manifest.get("version") != 1 or manifest.get("format") != "AQ5v1-chunks" or
            not isinstance(chunks, list) or not 1 <= len(chunks) <= 4096 or
            type(total) is not int or total < 70 or not isinstance(sha, str) or not re.fullmatch(r"[a-f0-9]{64}", sha)):
        raise ValueError("state_manifest_invalid")
    for index, chunk in enumerate(chunks):
        if (not isinstance(chunk, dict) or chunk.get("path") != f"state-part-{index:05d}.bin" or
                type(chunk.get("size")) is not int or not 1 <= chunk["size"] <= STATE_CHUNK_BYTES or
                not isinstance(chunk.get("sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", chunk["sha256"])):
            raise ValueError("state_chunk_descriptor_invalid")
    if sum(chunk["size"] for chunk in chunks) != total:
        raise ValueError("state_manifest_size_mismatch")
    result = bytearray()
    for chunk in chunks:
        value = _gh_raw(chunk["path"], ref=commit)
        if value is None or len(value) != chunk["size"] or hashlib.sha256(value).hexdigest() != chunk["sha256"]:
            raise ValueError("state_chunk_integrity_failed")
        result.extend(value)
    if hashlib.sha256(result).hexdigest() != sha or result[:5] != b"AQ5v1":
        raise ValueError("state_package_integrity_failed")
    return bytes(result)


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
    tmp = Path(tempfile.mkdtemp(prefix="cloud-state-", dir=DATA)) / "s.db"
    src = sqlite3.connect(DB_PATH.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
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
    from . import live
    live.init()
    key = _key()
    if not key:
        raise ValueError("缺少解密口令（AQ5_KEY / 手机查看密码）")
    d = json.loads(vault.unseal(blob, key))
    tmp = Path(tempfile.mkdtemp(prefix="cloud-state-", dir=DATA)) / "s.db"
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


def _state_owned(name: str) -> bool:
    return name in ("state.bin", STATE_MANIFEST, "cloud.json") or bool(re.fullmatch(r"state-part-\d{5}\.bin", name))


def _state_path(root: Path, name: str) -> Path:
    """Only app-owned direct children; refuse links escaping the staging root."""
    expected = DATA.resolve() / "state_repo"
    if root.resolve() != expected or root.is_symlink() or not _state_owned(name):
        raise ValueError("state_staging_path_invalid")
    result = root / name
    if result.resolve().parent != expected or result.is_symlink():
        raise ValueError("state_staging_path_invalid")
    return result


def _write_state_chunks(root: Path, blob: bytes) -> list[str]:
    if not isinstance(blob, bytes) or len(blob) < 70 or not blob.startswith(b"AQ5v1"):
        raise ValueError("state_package_invalid")
    if len(blob) > STATE_CHUNK_BYTES * 4096:
        raise ValueError("state_package_too_large")
    previous = {path.name for path in root.iterdir() if _state_owned(path.name)}
    chunks = []
    for index, start in enumerate(range(0, len(blob), STATE_CHUNK_BYTES)):
        part = blob[start:start + STATE_CHUNK_BYTES]
        name = f"state-part-{index:05d}.bin"
        _state_path(root, name).write_bytes(part)
        chunks.append({"path": name, "size": len(part), "sha256": hashlib.sha256(part).hexdigest()})
    manifest = {"version": 1, "format": "AQ5v1-chunks", "size": len(blob),
                "sha256": hashlib.sha256(blob).hexdigest(), "chunks": chunks}
    _state_path(root, STATE_MANIFEST).write_text(json.dumps(manifest, separators=(",", ":")), "utf-8")
    current = {STATE_MANIFEST, "cloud.json", *(part["path"] for part in chunks)}
    # Only obsolete encrypted transport files are removed from local staging.
    # The encrypted database contents (all included historical rows) are intact.
    for name in previous - current:
        path = _state_path(root, name)
        if not path.is_file():
            raise ValueError("state_staging_file_invalid")
        path.unlink()
    return sorted(current | previous)


def _safe_git_error(detail) -> str:
    text = str(detail or "")
    for name in ("GITHUB_TOKEN", "GH_TOKEN", "AQ5_KEY", "AQ5_AI_KEY"):
        secret = os.environ.get(name)
        if secret:
            text = text.replace(secret, "[redacted]")
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    text = re.sub(r"(?i)https?://[^\s<>\"']+", "[url]", text)
    text = re.sub(r"(?i)(?:github_pat_|gh[pousr]_)[a-z0-9_]+", "[redacted]", text)
    text = re.sub(r"(?i)(authorization|password|token)\s*[:=]\s*[^\r\n]+", r"\1=[redacted]", text)
    text = re.sub(r"[A-Za-z]:[\\/][^\r\n]*?(?=:\s|$)", "[path]", text, flags=re.MULTILINE)
    return " ".join(text.split())[:700]


def upload(active: bool, final: bool = False) -> dict:
    def failed(reason, detail=""):
        STATE["error"] = reason
        STATE["error_detail"] = _safe_git_error(detail) or None
        return {"ok": False, "error": reason, **({"detail": STATE["error_detail"]} if STATE["error_detail"] else {})}

    if final and (active or not db.writes_frozen()):
        return failed("最终状态只能从已冻结的账本上传")
    cfg = _cfg()
    if not cfg["remote"] or not _key():
        return {"ok": False, "error": "未配置发布仓库或手机查看密码"}
    with _lock:
        root = DATA / "state_repo"
        root.mkdir(exist_ok=True)
        _state_path(root, "cloud.json")
        if not (root / ".git").exists():
            setup = (["init", "-q"], ["checkout", "-q", "-b", BRANCH], ["config", "user.name", "aq5-state"],
                     ["config", "user.email", "aq5-state@users.noreply.github.com"])
            for step in setup:
                code, output = _git(step, root)
                if code:
                    return failed("状态仓库初始化失败", output)
        rc, remotes = _git(["remote"], root)
        if rc:
            return failed("状态仓库远端读取失败", remotes)
        verb = "set-url" if "origin" in remotes.split() else "add"
        code, output = _git(["remote", verb, "origin", cfg["remote"]], root)
        if code:
            return failed("状态仓库远端设置失败", output)
        ts = now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            paths = _write_state_chunks(root, pack())
        except Exception as exc:  # noqa: BLE001
            return failed(f"状态打包或分片失败（{type(exc).__name__}）")
        meta = {"owner": STATE["role"], "active": bool(active), "heartbeat": ts, "updated": ts,
                "state_format": "AQ5v1-chunks", "state_manifest": STATE_MANIFEST,
                "note": "A量化云端交接状态（完整加密状态按块保存）。请勿手工修改。"}
        _state_path(root, "cloud.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), "utf-8")
        rc, tracked = _git(["ls-files"], root)
        if rc:
            return failed("状态暂存文件读取失败", tracked)
        if any(not _state_owned(name) for name in tracked.splitlines()):
            return failed("状态仓库包含未识别文件，未提交或推送")
        tracked_paths = set(tracked.splitlines())
        paths = sorted({name for name in paths if _state_path(root, name).exists()} | tracked_paths)
        code, output = _git(["add", "-A", "--", *paths], root)
        if code:
            return failed("状态文件暂存失败", output)
        has_head = _git(["rev-parse", "--verify", "HEAD"], root)[0] == 0
        commit = ["commit", "-q", "--amend"] if has_head else ["commit", "-q"]
        code, output = _git(commit + ["-m", f"state {ts}"], root)
        if code:
            return failed("状态提交失败", output)
        code, output = _git(["push", "-q", "--force", "origin", f"HEAD:{BRANCH}"], root, timeout=300)
        if code:
            return failed("状态推送未确认", output)
        STATE["last_up"] = ts
        STATE["error"] = None
        STATE["error_detail"] = None
        if not CLOUD:
            save_settings({"cloud": {"last_sync": ts}})
        return {"ok": True, "time": ts}


# ----------------------------------------------------------------------------- 电脑端：启动交接 + 定时上传
def _unfinished(meta) -> bool:
    return bool(meta and meta.get("owner") == "cloud"
                and (meta.get("active") is not False or not meta.get("updated")))


def _reconcile_error(reason: str, waiting: bool = False) -> dict:
    STATE.update(error=reason, waiting=waiting)
    db.event("system", "云端交接未确认", reason, "warn")
    return {"error": reason, "waiting": waiting}


def reconcile(max_wait: int = 330) -> dict:
    """电脑启动时调用（在调度器、高频引擎启动之前）。"""
    cfg = _cfg()
    if CLOUD or not cfg["enabled"] or not cfg["repo"] or not _key():
        return {"skipped": True}
    try:
        meta = remote_meta()
        STATE["remote"] = meta
        observed_unfinished = _unfinished(meta)
        t0 = time.time()
        while _unfinished(meta) and _age(meta.get("heartbeat")) < 420 and time.time() - t0 < max_wait:
            STATE["waiting"] = True
            db.event("system", "云端正在代跑，等它交接…", "最多约 5 分钟", "info")
            time.sleep(20)
            meta = remote_meta()
            STATE["remote"] = meta
        confirmed_final = bool(meta and meta.get("owner") == "cloud" and not _unfinished(meta))
        if _unfinished(meta) or (observed_unfinished and not confirmed_final):
            return _reconcile_error("云端最终状态尚未确认，需等待交接成功或人工核验后恢复。", waiting=True)
        res = {"pulled": False}
        if confirmed_final and (meta.get("updated") or "") > (cfg["last_sync"] or ""):
            blob = read_remote_state(ref=meta.get("_state_commit"))
            confirmed_meta = remote_meta()
            STATE["remote"] = confirmed_meta
            if not blob or confirmed_meta != meta:
                return _reconcile_error("云端状态缺失或读取期间已变化，未接管。", waiting=True)
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
        if not upload(active=False).get("ok"):
            return _reconcile_error("电脑接管状态未能发布，已暂停自动任务。")
        STATE.update(waiting=False, error=None)
        return res
    except Exception as e:  # noqa: BLE001
        return _reconcile_error(f"云端交接读取或导入失败（{type(e).__name__}）。")


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
    next_reason = None
    while True:
        hm = now().hour * 100 + now().minute
        if hm >= GUARD_END:
            reason = "收盘后结束"
            break
        if time.time() - t0 > max_minutes * 60:
            reason = "单次运行接近 6 小时上限"
            next_reason = reason
            break
        alive = pc_alive()
        misses = 0 if alive else misses + 1
        if not active:
            if misses >= OFFLINE_CONFIRM:
                try:
                    _take_over(sim, hf, scheduler)
                except Exception as e:  # noqa: BLE001
                    STATE["error"] = "云端接管未确认"
                    print(f"云端接管未确认（{type(e).__name__}），未开启自动任务。", flush=True)
                    return 1
                active, last_up = True, time.time()
            elif misses == 0 and int(time.time() - t0) % 1800 < 60:
                print(now().strftime("%H:%M"), "电脑在线，云端待命", flush=True)
        else:
            if alive:
                reason = "电脑上线，交还给电脑"
                next_reason = "交还后继续值守"
                break
            if time.time() - last_up > 300:
                last_up = time.time()
                res = upload(active=True)
                print(now().strftime("%H:%M"), "状态已上传" if res.get("ok") else res, flush=True)
        time.sleep(60)
    if active:
        from . import live
        live.stop()
        db.event("system", f"云端代跑结束（{reason}）", "", "info")
        db.freeze_writes()
        try:
            res = upload(active=False, final=True)
        except Exception as e:  # noqa: BLE001
            STATE["error"] = "云端最终状态上传异常"
            print(f"云端最终状态未确认（{type(e).__name__}），交接失败。", flush=True)
            return 1
        if not res.get("ok"):
            STATE["error"] = "云端最终状态推送未确认"
            print("云端最终状态推送未确认，交接失败。", res, flush=True)
            return 1
        print("云端最终状态已上传，交接完成。", reason, flush=True)
    else:
        print("值守结束：", reason, "（期间电脑一直在线，云端没有交易）", flush=True)
    if next_reason:
        dispatch_next(next_reason)
    return 0


def _take_over(sim, hf, scheduler) -> None:
    """电脑确认离线：导入电脑最后上传的状态，开始交易和发布。"""
    meta = remote_meta()
    if not meta or _unfinished(meta):
        raise RuntimeError("上次云端状态未完成交接")
    blob = read_remote_state(ref=meta.get("_state_commit"))
    if not blob or remote_meta() != meta:
        raise RuntimeError("远端状态缺失或已变化")
    r = unpack(blob, take_settings=True)
    print(now().strftime("%H:%M"), f"电脑离线，云端接手。导入状态：{meta.get('owner')} {meta.get('updated')}，{r['tables']} 张表", flush=True)
    save_settings({"remote": {"enabled": False}, "backup": {"enabled": False}})  # 云端：不开隧道、不推私有备份；发布网页照常
    sec = {"mobile_password": os.environ.get("AQ5_KEY", "")}
    if os.environ.get("AQ5_AI_KEY"):
        sec["ai_key"] = os.environ["AQ5_AI_KEY"]
    vault.set_many(sec)
    sim.ensure_accounts()
    db.event("system", "云端代跑开始", "电脑离线超过 3 分钟，GitHub 云端接手：自动选股、AI 自动盘、计划模拟、自选信号、发布网页", "info")
    if not upload(active=True).get("ok"):
        raise RuntimeError("未能发布云端接管状态")
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
            blob = read_remote_state(ref=meta.get("_state_commit") if meta else None)
            d = json.loads(vault.unseal(blob, os.environ["AQ5_KEY"])) if blob else None
            print("AQ5_KEY：", "正确，能解开电脑上传的状态" if d else "加密状态还不存在", flush=True)
        except Exception as e:  # noqa: BLE001
            print("AQ5_KEY：错误 ——", e, "（请确认填的是电脑上设置的「手机查看密码」）", flush=True)
    else:
        print("AQ5_KEY：未设置", flush=True)
    print("AQ5_AI_KEY：", "已设置" if os.environ.get("AQ5_AI_KEY") else "未设置", flush=True)
    if os.environ.get("AQ5_AI_KEY") and meta:
        try:
            st = json.loads(vault.unseal(read_remote_state(ref=meta.get("_state_commit")), os.environ["AQ5_KEY"]))["settings"]
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
