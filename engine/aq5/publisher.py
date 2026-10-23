"""公开发布：把可以公开的数据导出为 JSON + 只读网页，提交并推送到 Git 仓库的 data 分支。

可以公开：大盘行情、市场温度、选股结果与候选、入选股/AI持仓的个股分析、AI自动盘成绩、因子迭代记录。
永远不导出：我的模拟盘（持仓/委托/成交）、自选股、推送 token、本机路径。

data 分支只保留一个提交（每次 amend + 强制推送），仓库不会越来越大；历史选股保存在 JSON 文件里。
手机可直接打开 GitHub Pages 网页，或让 Claude / Codex 插件读取 raw JSON。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from pathlib import Path

from . import db, market, picker, sim, vault
from .config import DATA, WEB, load_settings, now, today

STATE = {"last": None, "last_push": None, "error": None, "running": False}
_lock = threading.Lock()
PUBLIC_EVENT_KINDS = ("pick", "learn")
SCHEMA = 1


def repo_dir() -> Path:
    d = load_settings()["publish"].get("repo_dir")
    return Path(d) if d else DATA / "publish"


def _w(path: Path, obj) -> None:
    from .server import clean
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(clean(obj), ensure_ascii=False, separators=(",", ":")), "utf-8")
    tmp.replace(path)


def _ai_view() -> dict:
    v = sim.view("ai")
    for t in v.get("trades", []):
        t.pop("order_id", None)
    return v


def public_overview() -> dict:
    from .server import api_overview
    ov = api_overview({})
    ov.pop("lan", None)
    # 自选股不算隐私（只有“我的模拟盘”和设置是隐私），公开发布
    ov["accounts"] = {"ai": ov["accounts"]["ai"]}
    ov["events"] = public_events(30)
    ov["scheduler"] = {"started": ov["scheduler"].get("started"), "jobs": ov["scheduler"].get("jobs", {})}
    ov["picker"] = {"running": False}
    ov["static"] = True
    return ov


def public_events(limit=100) -> list[dict]:
    rows = db.q("SELECT * FROM events WHERE kind IN ('pick','learn') OR (kind='trade' AND title LIKE 'AI自动盘%') "
                "ORDER BY id DESC LIMIT ?", (limit,))
    return rows


def _analysis_codes() -> list[str]:
    codes = [r["code"] for r in db.q("SELECT DISTINCT code FROM picks WHERE date >= date(?, '-7 day')", (today(),))]
    codes += [r["code"] for r in db.q("SELECT code FROM positions WHERE account='ai'")]
    codes += [market.norm_code(c) for c in load_settings()["watchlist"]]
    seen, out = set(), []
    for c in codes:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out[:80]


def export(full: bool = True) -> dict:
    """写出所有公开文件到 repo_dir。full=False 只更新行情总览（盘中高频）。"""
    from .analysis import analyze
    from .server import api_learn, api_strategies
    root = repo_dir()
    d = root / "data"
    d.mkdir(parents=True, exist_ok=True)
    _site(root)
    files = []
    ov = public_overview()
    _w(d / "latest.json", ov)
    files.append("latest.json")
    for q in ov["indices"]:
        try:
            _w(d / "intraday" / f"{q['code']}.json", market.intraday(q["code"]))
        except RuntimeError:
            pass
    if full:
        runs = picker.runs(60)
        from . import ai
        for r in runs:
            r["cand_list"] = picker.top_candidates(r["id"], 30)
            n = ai.latest("run", str(r["id"]))
            r["ai"] = {"content": n["content"], "created": n["created"]} if n else None
        _w(d / "runs.json", runs)
        by_day = {}
        for r in runs:
            by_day.setdefault(r["date"], []).append(r)
        for day, rs in by_day.items():
            _w(d / "picks" / f"{day}.json", rs)
        _w(d / "picks" / "latest.json", runs[0] if runs else None)
        _w(d / "ai.json", _ai_view())
        _w(d / "learn.json", api_learn({}))
        _w(d / "strategies.json", api_strategies({}))
        try:
            from . import sectors
            _w(d / "sectors.json", sectors.view())
            files.append("sectors.json")
        except Exception as e:  # noqa: BLE001
            db.event("publish", "导出板块数据失败", str(e)[:150], "warn")
        _w(d / "events.json", public_events(200))
        s = load_settings()
        _w(d / "settings.json", {"pick": {k: s["pick"][k] for k in ("times", "max_picks", "min_score")},
                                 "risk": s["risk"], "emotions": sim.EMOTIONS, "static": True,
                                 "ai": {"provider": s.get("ai", {}).get("provider"), "model": s.get("ai", {}).get("model")}})
        idx = []
        for code in _analysis_codes():
            try:
                a = analyze(code)
                a["kline"] = market.kline_with_live(code, 250)
                _w(d / "analysis" / f"{code}.json", a)
                idx.append({"code": code, "name": a.get("name"), "score": a["score"], "rating": a["rating"],
                            "time": now().strftime("%Y-%m-%d %H:%M")})
            except (RuntimeError, ValueError) as e:
                db.event("publish", f"导出 {code} 分析失败", str(e), "warn")
        old = {}
        p = d / "analysis" / "index.json"
        if p.exists():
            try:
                old = {x["code"]: x for x in json.loads(p.read_text("utf-8"))}
            except (ValueError, KeyError):
                old = {}
        old.update({x["code"]: x for x in idx})
        _w(p, sorted(old.values(), key=lambda x: x["time"], reverse=True))
        files += ["runs.json", "ai.json", "learn.json", f"analysis/*.json({len(idx)})"]
    files += _private_pack(d, full)
    _w(d / "manifest.json", {"schema": SCHEMA, "updated": now().strftime("%Y-%m-%d %H:%M:%S"), "full": full,
                             "phase": market.market_phase(), "files": files,
                             "note": "A量化公开数据：行情、选股、个股分析、AI自动盘、因子迭代。仅供学习研究，不构成投资建议。"})
    return {"dir": str(root), "files": files}


_PRIV_CACHE: dict = {"analysis": {}}


def _private_pack(d: Path, full: bool) -> list[str]:
    """自选股 + 我的模拟盘 + 个人资料 → 用「手机查看密码」加密成 private.enc。
    仓库里只有密文；在 GitHub 网页上输入密码后由浏览器本地解密，服务器和 GitHub 都看不到明文。"""
    import gzip
    from .analysis import analyze
    from .server import clean
    pw = vault.get("mobile_password")
    target = d / "private.enc"
    if not pw:
        if target.exists():
            target.unlink()
        return []
    from .config import load_settings
    from . import auth
    wl = [market.norm_code(c) for c in load_settings()["watchlist"]]
    if full or not _PRIV_CACHE["analysis"]:
        for code in wl:
            try:
                a = analyze(code)
                a["kline"] = market.kline_with_live(code, 250)
                _PRIV_CACHE["analysis"][code] = a
            except (RuntimeError, ValueError):
                pass
    for code in list(_PRIV_CACHE["analysis"]):
        if code not in wl:
            _PRIV_CACHE["analysis"].pop(code)
    qs = market.quotes(wl)
    me = sim.view("me")
    u = auth.user() if auth.has_user() else None
    pack = {"time": now().strftime("%Y-%m-%d %H:%M:%S"), "watchlist": [qs.get(c) or {"code": c} for c in wl],
            "analysis": _PRIV_CACHE["analysis"], "me": me, "profile": (u or {}).get("profile", {}),
            "events": db.q("SELECT * FROM events WHERE kind IN ('alert','trade','ai') ORDER BY id DESC LIMIT 80"),
            "ai": _private_ai(wl), "lan": _lan(), "remote": _remote()}
    raw = gzip.compress(json.dumps(clean(pack), ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    target.write_bytes(vault.seal_web(raw, pw))
    return [f"private.enc({len(_PRIV_CACHE['analysis'])}只自选,加密)"]


def _remote():
    try:
        from . import tunnel
        return tunnel.remote_info()
    except Exception:  # noqa: BLE001
        return None


def _lan() -> str:
    try:
        from .server import lan_ip
        return f"http://{lan_ip()}:{load_settings()['port']}"
    except Exception:  # noqa: BLE001
        return ""


def _private_ai(wl: list[str]) -> dict:
    """个人相关的 AI 内容（含持仓/交易），只进加密包。"""
    from . import ai
    pick = lambda n: {"content": n["content"], "created": n["created"], "date": n["date"]} if n else None  # noqa: E731
    out = {"brief": pick(ai.latest("brief", "")), "review": pick(ai.latest("review", "")),
           "holdings": pick(ai.latest("holdings", "")), "comments": {}, "trades": {}}
    for c in wl:
        n = ai.latest("comment", c)
        if n:
            out["comments"][c] = {**pick(n), "meta": n["meta"]}
    for t in db.q("SELECT id FROM trades WHERE account='me' ORDER BY id DESC LIMIT 30"):
        n = ai.latest("trade", str(t["id"]))
        if n:
            out["trades"][str(t["id"])] = pick(n)
    return out


def _site(root: Path) -> None:
    """把只读网页复制到发布目录（GitHub Pages 用）。"""
    for sub in ("css", "js"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    for f in ("index.html", "manifest.json", "icon.svg", "css/app.css", "js/app.js", "js/charts.js", "js/lite.js"):
        src = WEB / f
        if src.exists():
            shutil.copyfile(src, root / f)
    (root / "js" / "static.js").write_text("window.AQ_STATIC = true;\n", "utf-8")
    html = (root / "index.html").read_text("utf-8")
    if "js/static.js" not in html:
        html = html.replace('<script src="js/charts.js"></script>',
                            '<script src="js/static.js"></script>\n<script src="js/charts.js"></script>')
        (root / "index.html").write_text(html, "utf-8")
    (root / ".nojekyll").write_text("", "utf-8")
    (root / "README.md").write_text(
        "# A量化 公开数据（data 分支）\n\n由本机 A量化 引擎自动生成并推送，请勿手工修改。\n\n"
        "- 网页：开启 GitHub Pages（分支 data，根目录）后访问 `https://<用户名>.github.io/<仓库>/`\n"
        "- 数据：`data/latest.json` 行情总览、`data/runs.json` 选股记录、`data/analysis/<代码>.json` 个股分析、"
        "`data/ai.json` AI自动盘、`data/learn.json` 因子迭代\n\n仅供学习研究，不构成投资建议。\n", "utf-8")


def _git(args: list[str], cwd: Path, timeout=90) -> tuple[int, str]:
    try:
        p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
        return p.returncode, (p.stdout + p.stderr).strip()
    except FileNotFoundError:
        return 127, "没有找到 git，请先安装 Git for Windows"
    except subprocess.TimeoutExpired:
        return 124, "git 超时"


def commit_push() -> dict:
    cfg = load_settings()["publish"]
    root = repo_dir()
    br = cfg["branch"]
    if not (root / ".git").exists():
        code, out = _git(["init"], root)
        if code:
            return {"ok": False, "error": out}
        _git(["checkout", "-b", br], root)
        _git(["config", "user.name", "aq5-publisher"], root)
        _git(["config", "user.email", "aq5-publisher@users.noreply.github.com"], root)
    rc, remotes = _git(["remote"], root)
    if "origin" not in remotes.split():
        _git(["remote", "add", "origin", cfg["remote"]], root)
    else:
        _git(["remote", "set-url", "origin", cfg["remote"]], root)
    _git(["add", "-A"], root)
    has_head = _git(["rev-parse", "--verify", "HEAD"], root)[0] == 0
    msg = f"data: {now().strftime('%Y-%m-%d %H:%M')}"
    code, out = _git(["commit", "-q", "--amend", "-m", msg] if has_head else ["commit", "-q", "-m", msg], root)
    if code and "nothing" not in out:
        return {"ok": False, "error": out}
    if not cfg.get("push", True):
        return {"ok": True, "pushed": False}
    code, out = _git(["push", "-q", "--force", "origin", f"HEAD:{br}"], root, timeout=120)
    if code:
        return {"ok": False, "error": out[-400:]}
    STATE["last_push"] = now().strftime("%Y-%m-%d %H:%M:%S")
    return {"ok": True, "pushed": True}


def publish(full: bool = True, force: bool = False) -> dict:
    cfg = load_settings()["publish"]
    if not (cfg.get("enabled") or force):
        return {"skipped": "未开启公开发布"}
    with _lock:
        if STATE["running"]:
            return {"skipped": "正在发布"}
        STATE["running"] = True
    try:
        t0 = time.time()
        ex = export(full)
        res = commit_push()
        STATE["last"] = now().strftime("%Y-%m-%d %H:%M:%S")
        STATE["error"] = None if res.get("ok") else res.get("error")
        if not res.get("ok"):
            db.event("publish", "推送到 Git 失败", res.get("error", ""), "warn")
        elif full:
            db.event("publish", "公开数据已发布", f"{', '.join(ex['files'])}，用时 {time.time() - t0:.0f} 秒")
        return {**ex, **res, "secs": round(time.time() - t0, 1)}
    finally:
        STATE["running"] = False


def publish_async(full=True, force=False):
    threading.Thread(target=lambda: _safe(full, force), daemon=True).start()


def _safe(full, force):
    try:
        publish(full, force)
    except Exception as e:  # noqa: BLE001
        STATE["error"] = str(e)
        db.event("publish", "发布出错", str(e)[:300], "error")

