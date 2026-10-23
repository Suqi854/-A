"""手机远程连接电脑引擎：用 Cloudflare 免费快速隧道（cloudflared）给本机引擎一个 https 地址。

- 不需要注册、不需要公网 IP；地址形如 https://xxxx.trycloudflare.com，每次启动会变。
- 新地址和一枚“手机专用 Token”会写进加密的个人数据包（private.enc），手机解锁后自动连上电脑，
  变成完整版（下单、改设置、AI、任意个股分析）。
- 经过隧道的请求一律按“外网”处理：必须登录或带 Token，本机免登录不生效。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
import urllib.request

from . import auth, db, vault
from .config import DATA, load_settings, now, save_settings

TOOLS = DATA / "tools"
EXE = TOOLS / ("cloudflared.exe" if sys.platform == "win32" else "cloudflared")
URLS = {
    "win32": "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe",
    "linux": "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64",
    "darwin": "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-amd64.tgz",
}
STATE = {"running": False, "connected": False, "url": None, "since": None, "error": None, "downloading": False, "progress": 0,
         "installed": False, "log": []}
_proc: dict = {"p": None}
_lock = threading.Lock()


def installed() -> bool:
    STATE["installed"] = EXE.exists() and EXE.stat().st_size > 1_000_000
    return STATE["installed"]


def download() -> dict:
    """由用户在设置页点击后执行：从 Cloudflare 官方 GitHub 发布页下载 cloudflared。"""
    if STATE["downloading"]:
        return {"skipped": "正在下载"}
    url = URLS.get(sys.platform)
    if not url or url.endswith(".tgz"):
        raise ValueError("这个系统请手动安装 cloudflared")

    def go():
        STATE.update(downloading=True, progress=0, error=None)
        try:
            TOOLS.mkdir(parents=True, exist_ok=True)
            tmp = EXE.with_suffix(".part")
            req = urllib.request.Request(url, headers={"User-Agent": "aq5"})
            with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
                total = int(r.headers.get("Content-Length") or 0)
                got = 0
                while True:
                    b = r.read(1 << 16)
                    if not b:
                        break
                    f.write(b)
                    got += len(b)
                    STATE["progress"] = round(got / total * 100) if total else got // 1_000_000
            os.replace(tmp, EXE)
            if sys.platform != "win32":
                os.chmod(EXE, 0o755)
            installed()
            db.event("system", "cloudflared 已下载", f"{EXE.stat().st_size // 1_000_000} MB")
            if load_settings().get("remote", {}).get("enabled"):
                start()
        except Exception as e:  # noqa: BLE001
            STATE["error"] = "下载失败：" + str(e)[:200]
        finally:
            STATE["downloading"] = False
    threading.Thread(target=go, daemon=True, name="aq5-cfdl").start()
    return {"started": True}


def phone_token() -> str:
    """手机专用 API Token（存本机保险箱，失效就重建）。"""
    tok = vault.get("phone_token")
    if tok and auth.check(tok):
        return tok
    tok = auth.create_api_token("手机远程（自动）", 180)
    vault.set_many({"phone_token": tok})
    return tok


def remote_info() -> dict | None:
    """写进加密包给手机用；未开启或隧道没起来就返回 None。"""
    if not load_settings().get("remote", {}).get("enabled") or not STATE["url"] or not auth.has_user():
        return None
    return {"url": STATE["url"], "token": phone_token(), "since": STATE["since"]}


def _reader(p):
    pat = re.compile(r"https://[-a-z0-9]+\.trycloudflare\.com")
    for line in iter(p.stderr.readline, b""):
        t = line.decode("utf-8", "ignore").strip()
        if not t:
            continue
        STATE["log"] = (STATE["log"] + [t[-200:]])[-30:]
        if "Registered tunnel connection" in t:
            STATE.update(connected=True, error=None)
        elif "Unregistered tunnel connection" in t or ("ERR" in t and "connIndex=0" in t):
            STATE["connected"] = False
            if "quic" in t.lower():
                STATE["error"] = "QUIC(UDP) 连不上 Cloudflare，已改用 http2(TCP)"
        m = pat.search(t)
        if m and m.group(0) != STATE["url"]:
            STATE.update(url=m.group(0), since=now().strftime("%Y-%m-%d %H:%M:%S"), error=None)
            db.event("system", "手机远程地址已更新", m.group(0))
            try:  # 尽快把新地址写进加密包
                from . import publisher
                if load_settings().get("publish", {}).get("enabled"):
                    publisher.publish_async(full=False, force=True)
            except Exception:  # noqa: BLE001
                pass


PIDFILE = TOOLS / "cloudflared.pid"


def _kill_pid(pid: int) -> None:
    try:
        if sys.platform == "win32":
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=10, creationflags=flags)
        else:
            os.kill(pid, 15)
    except Exception:  # noqa: BLE001
        pass


def cleanup_stale() -> None:
    """引擎启动时：把上一次留下的 cloudflared 进程关掉（引擎重启时子进程不会自动退出）。
    只关从本程序 data\\tools 目录启动的 cloudflared，不影响你自己运行的其他 cloudflared。"""
    if sys.platform == "win32":
        ps = ("Get-CimInstance Win32_Process -Filter \"Name='cloudflared.exe'\" | "
              f"Where-Object {{ $_.ExecutablePath -eq '{EXE}' }} | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force }}")
        try:
            subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, timeout=20,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception:  # noqa: BLE001
            pass
    try:
        pid = int(PIDFILE.read_text().strip())
    except (OSError, ValueError):
        return
    _kill_pid(pid)
    try:
        PIDFILE.unlink()
    except OSError:
        pass


def start(only_if_enabled: bool = False) -> dict:
    with _lock:
        if only_if_enabled and not load_settings().get("remote", {}).get("enabled"):
            return {"running": False}
        if _proc["p"] and _proc["p"].poll() is None:
            return {"running": True, "url": STATE["url"]}
        if not installed():
            raise ValueError("还没有 cloudflared，先点「下载」")
        port = load_settings()["port"]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        # 用 http2（TCP 443）而不是默认的 QUIC（UDP）：代理软件 TUN 模式、公司/校园网常常拦 UDP
        proto = load_settings().get("remote", {}).get("protocol", "http2")
        p = subprocess.Popen([str(EXE), "tunnel", "--no-autoupdate", "--protocol", proto, "--edge-ip-version", "auto",
                              "--url", f"http://127.0.0.1:{port}"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, creationflags=flags)
        _proc["p"] = p
        try:
            PIDFILE.write_text(str(p.pid))
        except OSError:
            pass
        STATE.update(running=True, connected=False, url=None, error=None)
        threading.Thread(target=_reader, args=(p,), daemon=True, name="aq5-cfread").start()
        return {"running": True}


def stop() -> dict:
    with _lock:
        p = _proc["p"]
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(5)
            except Exception:  # noqa: BLE001
                _kill_pid(p.pid)
        _proc["p"] = None
        cleanup_stale()
        STATE.update(running=False, connected=False, url=None)
    return {"running": False}


def set_enabled(on: bool) -> dict:
    save_settings({"remote": {"enabled": bool(on)}})
    if on:
        if installed():
            start()
    else:
        stop()
    return status()


def status() -> dict:
    installed()
    p = _proc["p"]
    STATE["running"] = bool(p and p.poll() is None)
    return {k: v for k, v in STATE.items() if k != "log"} | {"enabled": bool(load_settings().get("remote", {}).get("enabled")),
                                                            "log": STATE["log"][-6:], "exe": str(EXE)}


def watchdog() -> None:
    """后台守护：开启状态下隧道进程退出就 30 秒后重启。"""
    def loop():
        while True:
            try:
                if load_settings().get("remote", {}).get("enabled") and installed():
                    p = _proc["p"]
                    if not p or p.poll() is not None:
                        start(only_if_enabled=True)
            except Exception as e:  # noqa: BLE001
                STATE["error"] = str(e)[:200]
            time.sleep(30)
    threading.Thread(target=loop, daemon=True, name="aq5-cfwatch").start()
