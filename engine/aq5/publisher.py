"""公开发布：把可以公开的数据导出为 JSON + 只读网页，提交并推送到 Git 仓库的 data 分支。

可以公开：大盘行情、市场温度、选股结果与候选、入选股/AI持仓的个股分析、AI自动盘成绩、因子迭代记录。
永远不导出：我的模拟盘（持仓/委托/成交）、自选股、推送 token、本机路径。

data 分支只保留一个提交（每次 amend + 强制推送），仓库不会越来越大；历史选股保存在 JSON 文件里。
手机可直接打开 GitHub Pages 网页，或让 Claude / Codex 插件读取 raw JSON。
"""
from __future__ import annotations

import hashlib
import json
import math
import shutil
import subprocess
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory

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
    # Private watchlists are exposed only through the encrypted package.
    ov["watchlist"] = []
    ov["accounts"] = {"ai": ov["accounts"]["ai"]}
    ov["events"] = public_events(30)
    ov["scheduler"] = {"started": ov["scheduler"].get("started"), "jobs": ov["scheduler"].get("jobs", {})}
    ov["picker"] = {"running": False}
    ov["static"] = True
    return ov


def public_events(limit=100) -> list[dict]:
    rows = db.q("SELECT * FROM events WHERE kind IN ('pick','learn','market') OR (kind='trade' AND title LIKE 'AI自动盘%') "
                "ORDER BY id DESC LIMIT ?", (limit,))
    return rows


def _analysis_codes(prioritize: list[str]) -> list[str]:
    codes = list(prioritize)
    codes += [r["code"] for r in db.q("SELECT code FROM picks WHERE date >= date(?, '-7 day') "
                                      "GROUP BY code ORDER BY MAX(date) DESC, code", (today(),))]
    codes += [r["code"] for r in db.q("SELECT code FROM positions WHERE account='ai'")]
    seen, out = set(), []
    for c in codes:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out[:80]


def _read_public(path: Path, default):
    try:
        value = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


def _run_assets(d: Path, latest: dict | None, full: bool) -> list[dict]:
    from . import ai

    runs_file = d / "runs.json"
    prior = _read_public(runs_file, [])
    if full:
        runs, refresh = picker.runs(60), True
        update = runs
    else:
        runs, refresh = prior, False
        update = []
        if latest:
            old = next((r for r in runs if r["id"] == latest["id"]), None)
            if old is None or "cand_list" not in old:
                update = [dict(latest)]
                runs = sorted(update + [r for r in runs if r["id"] != latest["id"]],
                              key=lambda r: (r["date"], r["id"]), reverse=True)[:60]
                refresh = True
    for r in update:
        r["cand_list"] = picker.top_candidates(r["id"], 30)
        n = ai.latest("run", str(r["id"]))
        r["ai"] = {"content": n["content"], "created": n["created"]} if n else None
    if refresh:
        _w(runs_file, runs)
    by_day = {}
    for r in runs if full else runs[:1]:
        by_day.setdefault(r["date"], [])
    for day in by_day:
        path = d / "picks" / f"{day}.json"
        old_day = _read_public(path, [])
        merged = {r["id"]: r for r in old_day} if not full else {}
        merged.update((r["id"], r) for r in runs if r["date"] == day)
        rows = sorted(merged.values(), key=lambda r: r["id"], reverse=True)
        if rows != old_day:
            _w(path, rows)
    first = runs[0] if runs else None
    latest_file = d / "picks" / "latest.json"
    if full or first is not None and _read_public(latest_file, {}) != first:
        _w(latest_file, first)
    return runs


def _usable_analysis_kline(rows, minimum_date: str = "") -> bool:
    return isinstance(rows, list) and len(rows) >= 30 and all(
        isinstance(r, dict) and isinstance(r.get("date"), str) and len(r["date"]) == 10
        and type(r.get("close")) in (int, float) and math.isfinite(r["close"]) and r["close"] > 0
        for r in rows[-30:]
    ) and rows[-1]["date"] >= minimum_date


def _analysis_assets(d: Path, latest: dict | None, full: bool, runs: list[dict]) -> int:
    from .analysis import analyze

    path = d / "analysis" / "index.json"
    current = {x["code"]: x for x in _read_public(path, [])}
    allowed = {r["code"] for r in db.q("SELECT DISTINCT code FROM picks UNION SELECT code FROM positions WHERE account='ai' "
                                       "UNION SELECT code FROM trades WHERE account='ai'")}
    allowed.update(item["code"] for r in runs for item in r.get("items", []))
    indexed = {code: item for code, item in current.items()
               if code in allowed and (path.parent / f"{code}.json").is_file()}
    latest_codes = list(dict.fromkeys(p["code"] for p in latest["items"])) if latest else []
    codes = _analysis_codes(latest_codes) if full else latest_codes[:80]
    cutoff = f"{latest['date']} {latest.get('finished') or latest.get('started') or '00:00:00'}" if latest else ""
    generated = 0
    for code in codes:
        asset = d / "analysis" / f"{code}.json"
        entry = indexed.get(code, {})
        latest_date = latest["date"] if code in latest_codes else ""
        if not full and asset.is_file() and (entry.get("pick_run_id") == latest["id"] or entry.get("time", "") >= cutoff):
            if _usable_analysis_kline(_read_public(asset, {}).get("kline"), latest_date):
                continue
        try:
            a = analyze(code)
        except (RuntimeError, ValueError, KeyError, TypeError) as e:
            db.event("publish", f"导出 {code} 分析失败", str(e), "warn")
            continue
        try:
            rows = market.kline_with_live(code, 250)
        except (RuntimeError, ValueError):
            rows = []
        if not _usable_analysis_kline(rows, latest_date):
            db.event("publish", f"暂未补齐 {code} 分析", "缺少足够的新日期日K，保留已有分析并等待下次发布重试。", "warn")
            continue
        a["kline"] = rows
        a["analysis_time"] = now().strftime("%Y-%m-%d %H:%M:%S")
        _w(asset, a)
        entry = {"code": code, "name": a.get("name"), "score": a["score"], "rating": a["rating"],
                 "time": a["analysis_time"]}
        if code in latest_codes:
            entry["pick_run_id"] = latest["id"]
        indexed[code] = entry
        generated += 1
    for stale in path.parent.glob("*.json"):
        if stale != path and stale.stem not in allowed:
            stale.unlink()
    if not path.exists() or indexed != current:
        _w(path, sorted(indexed.values(), key=lambda x: x["time"], reverse=True))
    return generated


def _quote_assets(d: Path, runs: list[dict], ov: dict) -> None:
    from .research import _quote_stamp

    def valid(row):
        px = row.get("price")
        return type(px) in (int, float) and math.isfinite(px) and px > 0

    path = d / "quotes.json"
    prev = _read_public(path, {})
    previous = prev.get("quotes") or {}
    fields = ("code", "price", "pct", "date", "time", "source")
    public = {p["code"] for r in runs for p in r.get("items", []) + r.get("cand_list", [])}
    quotes = {code: {k: previous[code].get(k) for k in fields}
              for code in public if code in previous and valid(previous[code])}
    try:
        snap = market.snapshot() if public else []
    except RuntimeError:
        snap = []
    primary = market.SNAP_INFO.get("source")
    incoming = [(q["code"], q, "腾讯" if _quote_stamp(q) else "东财快照" if primary == "eastmoney" else "市场快照")
                for q in snap if q["code"] in public]
    incoming += [(code, q, "腾讯" if _quote_stamp(q) else "市场快照")
                 for code, q in ov.get("pick_quotes", {}).items() if code in public]
    recent = runs[0] if runs else {}
    covered = {code for code, q, _ in incoming if valid(q) and
               (_quote_stamp(q) or not _quote_stamp(quotes.get(code, {})))}
    missing = sorted({p["code"] for p in recent.get("items", []) + recent.get("cand_list", [])} - covered)
    if missing:
        try:
            incoming += [(code, q, "腾讯") for code, q in market.quotes(missing[:40]).items() if code in public]
        except (RuntimeError, ValueError):
            pass
    for code, row, source in incoming:
        if not valid(row):
            continue
        observed = _quote_stamp(row)
        old_observed = _quote_stamp(quotes.get(code, {}))
        if old_observed and (not observed or observed < old_observed):
            continue
        if market.MOCK:
            source = "模拟行情"
        elif not observed:
            source += "（行情时间未提供）"
        change = row.get("pct")
        quotes[code] = {"code": code, "price": row["price"],
                        "pct": change if type(change) in (int, float) and math.isfinite(change) else None,
                        "date": observed[:10] if observed else None,
                        "time": observed[:19].replace("T", " ") if observed else None,
                        "source": source}
    changed = any(previous.get(code) != q for code, q in quotes.items())
    _w(path, {"time": now().strftime("%Y-%m-%d %H:%M:%S") if changed else prev.get("time"), "quotes": quotes})


def export(full: bool = True) -> dict:
    """盘中增量同步最新选股、公开报价及缺失分析；空目录先建立公开文件。"""
    from .server import api_learn, api_strategies
    root = repo_dir()
    d = root / "data"
    d.mkdir(parents=True, exist_ok=True)
    full = full or not (d / "runs.json").is_file()
    _site(root)
    files = []
    ov = public_overview()
    files += ["latest.json", "runs.json", "quotes.json"]
    runs = _run_assets(d, ov.get("last_run"), full)
    _quote_assets(d, runs, ov)
    try:
        from . import marketstate
        _w(d / "market.json", marketstate.state())
    except Exception:  # noqa: BLE001
        pass
    for q in ov["indices"]:
        try:
            _w(d / "intraday" / f"{q['code']}.json", market.intraday(q["code"]))
        except RuntimeError:
            pass
    if full:
        from . import research
        _w(d / "research.json", research.preview())
        _w(d / "ai.json", _ai_view())
        _w(d / "learn.json", api_learn({}))
        _w(d / "strategies.json", api_strategies({}))
        try:
            from . import signals, tracking
            _w(d / "track.json", tracking.track())
            _w(d / "t.json", {"stats": signals.t_stats(), "recent": db.q("SELECT * FROM t_signals WHERE account='ai' ORDER BY id DESC LIMIT 60")})
            files += ["track.json", "t.json"]
        except Exception as e:  # noqa: BLE001
            db.event("publish", "导出跟踪数据失败", str(e)[:150], "warn")
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
                                 "ai": {"provider": s.get("ai", {}).get("provider"), "model": s.get("ai", {}).get("model")},
                                 "look": s.get("look") or {}})  # 外观（皮肤等）各端同步，不含个人数据
        files += ["ai.json", "learn.json"]
    generated = _analysis_assets(d, ov.get("last_run"), full, runs)
    if generated:
        files.append(f"analysis/*.json({generated})")
    files += _private_pack(d, full)
    private_file = d / "private.enc"
    ov["private_pack_sha256"] = hashlib.sha256(private_file.read_bytes()).hexdigest() if private_file.exists() else None
    _w(d / "latest.json", ov)
    _w(d / "manifest.json", {"schema": SCHEMA, "updated": now().strftime("%Y-%m-%d %H:%M:%S"), "full": full,
                             "phase": market.market_phase(), "files": files,
                             "note": "A量化公开数据：行情、选股、个股分析、AI自动盘、因子迭代。仅供学习研究，不构成投资建议。"})
    return {"dir": str(root), "files": files}


_PRIV_CACHE: dict = {"analysis": {}}


def _private_pack(d: Path, full: bool) -> list[str]:
    """自选股 + 我的模拟盘 + 计划模拟盘 + 个人资料 → 用「手机查看密码」加密成 private.enc。
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
            "events": db.q("SELECT * FROM events WHERE kind IN ('alert','trade','ai','announce') ORDER BY id DESC LIMIT 80"),
            "ai": _private_ai(wl), "lan": _lan(), "remote": _remote(), "signals": _private_signals(wl),
            "plan": _private_plan(), **_private_strategy(), "look": load_settings().get("look") or {}}
    raw = gzip.compress(json.dumps(clean(pack), ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    target.write_bytes(vault.seal_web(raw, pw))
    return [f"private.enc({len(_PRIV_CACHE['analysis'])}只自选,加密)"]


def _private_plan():
    """计划模拟盘（持仓、策略、成交）只进加密包：电脑离线时手机解锁后也能看。"""
    try:
        from . import plan
        v = plan.view()
        return v if v.get("exists") else None
    except Exception:  # noqa: BLE001
        return None


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


def _private_signals(codes) -> dict:
    """自选股的当日信号（含我的持仓信息，只放进加密包）。"""
    out = {}
    try:
        from . import signals
        for c in codes:
            ph = signals.phases(c)
            if ph:
                order = {"post": 3, "intra": 2, "pre": 1}
                latest = max(ph.values(), key=lambda d: (order.get(d.get("phase"), 0), d.get("time", "")))
                out[c] = {"latest": latest, "phases": ph}
    except Exception:  # noqa: BLE001
        pass
    return out


def _site(root: Path) -> None:
    """把只读网页复制到发布目录（GitHub Pages 用）。"""
    for sub in ("css", "js"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    for f in ("index.html", "manifest.json", "icon.svg", "css/app.css", "css/themes.css", "css/mobile.css", "js/app.js", "js/charts.js", "js/lite.js"):
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


def _inherit_public_branch(root: Path, cfg: dict) -> None:
    """In a fresh publish directory, preserve the public artifacts already on the data branch."""
    remote, branch = cfg.get("remote"), cfg["branch"]
    if not cfg.get("push", True) or not remote:
        return
    root.mkdir(parents=True, exist_ok=True)
    if (root / ".git").exists():
        rc, _ = _git(["rev-parse", "--verify", "HEAD"], root)
        if rc:
            raise RuntimeError("本地公开发布基线不完整，停止本次推送")
        return
    if any(root.iterdir()):
        return
    rc, _ = _git(["ls-remote", "--exit-code", "--heads", "--", remote, f"refs/heads/{branch}"], root)
    if rc == 2:  # The branch does not exist, so there is no previous publication to preserve.
        return
    if rc:
        raise RuntimeError("无法确认远端公开发布基线，停止本次推送")
    with TemporaryDirectory(dir=root.parent, prefix="public-baseline-") as staging:
        clone = Path(staging) / "data-branch"
        rc, _ = _git(["clone", "--depth=1", "--single-branch", "--branch", branch, "--", remote, str(clone)],
                     Path(staging))
        if rc:
            raise RuntimeError("无法继承远端公开发布基线，停止本次推送")
        root.rmdir()
        clone.rename(root)


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
        _inherit_public_branch(repo_dir(), cfg)
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



def _private_strategy():
    from . import intraday, training
    return {"intraday": intraday.latest(), "intraday_status": intraday.status(), "training": training.view()}
