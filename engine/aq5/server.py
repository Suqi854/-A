"""HTTP 服务（标准库），提供 JSON API 和网页。电脑、手机浏览器都能访问。"""
from __future__ import annotations

import json
import math
import mimetypes
import socket
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import ai, analysis, auth, backtest, backup, db, learn, market, notify, picker, publisher, scheduler, sim, vault, sectors, tunnel, signals, tracking, hf, marketstate
from .config import DATA, MOCK, WEB, load_settings, now, public_settings, save_settings
from .strategies import DESC, NAMES

VERSION = "5.0.0"


def lan_ip() -> str:
    """手机能访问的局域网地址：优先 192.168 / 10 / 172.16-31，排除代理软件的虚拟网卡（198.18.x 等）。"""
    cands = []
    try:
        cands += [i[4][0] for i in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)]
    except OSError:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        cands.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass

    def rank(ip):
        if ip.startswith("192.168.137."):  # Windows 移动热点网卡，通常不是手机所在的网
            return 5
        if ip.startswith("192.168."):
            return 0
        if ip.startswith("10."):
            return 1
        p = ip.split(".")
        if p[0] == "172" and 16 <= int(p[1]) <= 31:
            return 2
        return 9
    good = sorted({c for c in cands if not c.startswith(("127.", "198.18.", "198.19.", "169.254."))}, key=rank)
    return good[0] if good else "127.0.0.1"


def clean(o):
    if isinstance(o, float):
        return None if (math.isnan(o) or math.isinf(o)) else round(o, 6)
    if isinstance(o, dict):
        return {str(k): clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    return o


# ----------------------------------------------------------------------------- API 实现
def api_overview(_q):
    s = load_settings()
    idx = market.quotes(s["indices"])
    ovs = market.quotes(s["overseas"])
    wl = market.quotes(s["watchlist"])
    try:
        snap = market.snapshot()
        br = market.breadth(snap)
        movers = sorted([r for r in snap if r.get("pct") is not None], key=lambda r: -(r["pct"] or 0))
        top = [{k: r.get(k) for k in ("code", "name", "price", "pct", "vr", "turnover", "main_net")} for r in movers[:12]]
        bottom = [{k: r.get(k) for k in ("code", "name", "price", "pct", "vr", "turnover", "main_net")}
                  for r in movers[-8:][::-1]]
        inflow = sorted([r for r in snap if r.get("main_net") is not None], key=lambda r: -r["main_net"])[:10]
        inflow = [{k: r.get(k) for k in ("code", "name", "price", "pct", "main_net", "main_pct")} for r in inflow]
    except RuntimeError as e:
        br, top, bottom, inflow = {"error": str(e)}, [], [], []
    try:
        reg = analysis.regime()
    except Exception:  # noqa: BLE001
        reg = {"state": "未知", "code": "unknown", "desc": "指数数据获取失败"}
    last = db.q1("SELECT id FROM pick_runs ORDER BY id DESC LIMIT 1")
    accts = {k: sim.view(k, light=True) for k in sim.ACCOUNTS}
    for a in accts.values():
        a["positions"] = [{k: p[k] for k in ("code", "name", "qty", "price", "pct", "pnl", "pnl_pct", "to_stop",
                                             "to_target")} for p in a["positions"]]
    return {"time": now().strftime("%Y-%m-%d %H:%M:%S"), "phase": market.market_phase(), "mock": MOCK,
            "indices": [idx.get(market.norm_code(c)) for c in s["indices"] if idx.get(market.norm_code(c))],
            "overseas": [ovs.get(c) for c in s["overseas"] if ovs.get(c)],
            "watchlist": [wl.get(market.norm_code(c)) or {"code": market.norm_code(c)} for c in s["watchlist"]],
            "breadth": br, "snap": market.SNAP_INFO, "regime": reg, "top": top, "bottom": bottom, "inflow": inflow,
            "last_run": picker.runs(1)[0] if last else None, "accounts": accts,
            "events": db.q("SELECT * FROM events ORDER BY id DESC LIMIT 30"),
            "scheduler": scheduler.STATUS, "picker": picker.STATE, "publish": publisher.STATE,
            "lan": f"http://{lan_ip()}:{s['port']}", "version": VERSION}


def api_quotes(q):
    codes = [c for c in (q.get("codes", [""])[0]).split(",") if c]
    return market.quotes(codes)


def api_kline(q):
    code = q["code"][0]
    n = int(q.get("n", ["250"])[0])
    period = q.get("period", ["day"])[0]
    if period != "day":
        return market.kline_period(code, period, min(n, 640))
    rows = market.kline_with_live(code, n)
    return {"code": market.norm_code(code), "rows": rows, "period": "day"}


def api_intraday(q):
    return market.intraday(q["code"][0])


def api_analyze(q):
    return analysis.analyze(q["code"][0])


def api_search(q):
    return market.search(q.get("q", [""])[0])


def api_runs(q):
    return picker.runs(int(q.get("limit", ["20"])[0]))


def api_candidates(q):
    return picker.top_candidates(int(q["run"][0]), 30)


def api_pick_state(_q):
    return picker.STATE


def api_sim(q):
    return sim.view(q.get("acct", ["me"])[0])


def api_learn(_q):
    out = learn.summary()
    from .strategies import STRATEGIES
    from .strategies import FORMULAS as SF
    out["strategies"] = [{"id": k, "name": n, "desc": d, "formula": SF.get(k, "")} for k, n, _, d in STRATEGIES]
    out["boot"] = learn.BOOT
    out["history_samples"] = (db.q1("SELECT COUNT(*) n FROM samples WHERE session='history'") or {}).get("n", 0)
    return out


def api_backtest(q):
    return backtest.run(q["code"][0], q.get("strategy", ["score"])[0], int(q.get("days", ["500"])[0]),
                        float(q.get("threshold", ["68"])[0]))


def api_settings(_q):
    s = public_settings()
    s["emotions"] = sim.EMOTIONS
    return s


def api_events(q):
    return db.q("SELECT * FROM events ORDER BY id DESC LIMIT ?", (int(q.get("limit", ["100"])[0]),))


def api_strategies(_q):
    return [{"id": k, "name": NAMES[k], "desc": DESC[k]} for k in NAMES]


REQ = threading.local()  # 当前请求的 ip / 会话，供个人接口使用

VAULT_KEYS = {"ai_key": "AI 接口 Key", "pushplus_token": "PushPlus token", "serverchan_key": "Server酱 SendKey",
              "backup_password": "备份密码", "mobile_password": "手机查看密码（GitHub 网页上解锁自选股和个人数据）",
              "github_token": "GitHub token（可选）"}
RISK_PRESET = {"保守": {"pick": {"position_pct": 0.1, "min_score": 75}, "risk": {"max_stop_pct": 0.06}},
               "均衡": {"pick": {"position_pct": 0.2, "min_score": 72}, "risk": {"max_stop_pct": 0.08}},
               "积极": {"pick": {"position_pct": 0.25, "min_score": 70}, "risk": {"max_stop_pct": 0.10}}}


def api_me(_q):
    s = load_settings()
    return {"user": auth.user(), "vault": vault.status(), "vault_keys": VAULT_KEYS, "sessions": auth.sessions(),
            "backup": {**s.get("backup", {}), **backup.STATE}, "security": s.get("security", {}),
            "local": getattr(REQ, "local", False)}


def p_profile(b):
    prof = auth.save_profile(b)
    if b.get("risk") in RISK_PRESET:
        save_settings(RISK_PRESET[b["risk"]])
    return prof


def p_vault(b):
    mp = str(b.get("mobile_password") or "")
    if mp and len(mp) < 8:
        raise ValueError("手机查看密码至少 8 位")
    vault.set_many({k: v for k, v in b.items() if k in VAULT_KEYS})
    return vault.status()


def p_password(b):
    auth.change_password(b.get("old", ""), b.get("new", ""))
    return {"ok": True, "message": "密码已修改，其他设备需要重新登录"}


def p_revoke(b):
    auth.revoke(b["id"])
    return {"ok": True}


def p_api_token(b):
    tok = auth.create_api_token(b.get("name", "插件"), int(b.get("days", 365)))
    return {"token": tok, "message": "只显示这一次，请马上复制保存"}


def p_backup(b):
    return backup.backup(push=bool(b.get("push", True)))


def p_restore(b):
    return backup.restore(b.get("password") or None)


def p_security(b):
    if not getattr(REQ, "local", False):
        raise ValueError("只能在电脑本机修改安全设置")
    return save_settings({"security": {"local_no_login": bool(b.get("local_no_login", True))},
                          "backup": {k: b[k] for k in ("enabled", "remote") if k in b}})["security"]


def api_ai_status(_q):
    return {"cfg": ai.cfg(), "ready": ai.ready(), "usage": ai.usage(), "presets": ai.PRESETS,
            "review": ai.latest("review", ""), "suggest": ai.latest("suggest", ""),
            "reviews": db.q("SELECT id, date, created, substr(content,1,60) AS head FROM ai_notes WHERE kind='review' "
                            "ORDER BY id DESC LIMIT 20")}


def api_ai_comment(q):
    code = q["code"][0]
    if q.get("cached", ["0"])[0] == "1":
        return ai.latest("comment", market.norm_code(code)) or {}
    return ai.comment(code, q.get("refresh", ["0"])[0] == "1")


def api_ai_news(q):
    a = q["code"][0]
    name = (db.q1("SELECT name FROM universe WHERE code=?", (market.norm_code(a),)) or {}).get("name")
    return ai.fetch_news(a, name)


def api_ai_note(q):
    r = db.q1("SELECT * FROM ai_notes WHERE id=?", (int(q["id"][0]),))
    return r or {}


def p_ai_config(b):
    allowed = {k: b[k] for k in ("provider", "base_url", "model", "format", "daily_limit", "auto_review",
                                  "auto_sentiment", "temperature", "auto_explain", "auto_brief") if k in b}
    return save_settings({"ai": allowed})["ai"]


def p_ai_ask(b):
    return ai.ask(b["code"], b["question"])


def api_ai_models(q):
    g = lambda k: (q.get(k) or [None])[0]  # noqa: E731
    return ai.list_models(g("provider"), g("base_url"), g("format"))


def api_ai_notes(q):
    kinds = tuple((q.get("kinds") or ["brief,review,run,holdings"])[0].split(","))
    return ai.notes(kinds, int((q.get("limit") or [30])[0]))


def api_ai_run(q):
    r = ai.latest("run", q["id"][0])
    return r or {}


def api_ai_trade(q):
    return ai.latest("trade", q["id"][0]) or {}


def api_sector(q):
    return sectors.detail(q["code"][0])


GET = {"/api/ai/status": api_ai_status, "/api/ai/comment": api_ai_comment, "/api/ai/news": api_ai_news,
       "/api/ai/note": api_ai_note, "/api/me": api_me, "/api/overview": api_overview, "/api/quotes": api_quotes, "/api/kline": api_kline,
       "/api/intraday": api_intraday, "/api/analyze": api_analyze, "/api/search": api_search,
       "/api/picks/runs": api_runs, "/api/picks/candidates": api_candidates, "/api/picks/state": api_pick_state,
       "/api/sim": api_sim, "/api/learn": api_learn, "/api/backtest": api_backtest, "/api/settings": api_settings,
       "/api/events": api_events, "/api/strategies": api_strategies,
       "/api/sectors": lambda q: sectors.view(), "/api/sector": api_sector, "/api/ai/models": api_ai_models,
       "/api/ai/notes": api_ai_notes, "/api/ai/run": api_ai_run, "/api/ai/trade": api_ai_trade,
       "/api/remote/status": lambda q: tunnel.status(),
       "/api/market/state": lambda q: marketstate.state(),
       "/api/market/accuracy": lambda q: marketstate.accuracy(),
       "/api/hf/status": lambda q: hf.status(),
       "/api/watch/signals": lambda q: signals.latest(),
       "/api/watch/signal": lambda q: {"latest": signals.latest(q["code"][0]), "phases": signals.phases(q["code"][0])},
       "/api/track": lambda q: tracking.track(int((q.get("days") or [20])[0])),
       "/api/t/stats": lambda q: {"stats": signals.t_stats(), "recent": db.q("SELECT * FROM t_signals ORDER BY id DESC LIMIT 60")},
       "/api/diag/sources": lambda q: {"stats": market.SRC_STATS, "blocked": market.host_status(), "snap": market.SNAP_INFO},
       "/api/diag/net": lambda q: {"probes": market.probe(q.get("raw", ["0"])[0] == "1",
                                                     {"1": True, "0": False}.get(q.get("direct", [""])[0]),
                                                     q.get("url"), int((q.get("n") or [600])[0])), "blocked": market.host_status(),
                                   "snap": market.SNAP_INFO}}


def p_pick_run(b):
    if picker.STATE["running"]:
        return {"started": False, "message": "选股正在进行中"}
    threading.Thread(target=lambda: picker.run(b.get("session", "manual")), daemon=True).start()
    return {"started": True}


def p_order(b):
    return sim.place(b.get("acct", "me"), b["code"], b["side"], int(b["qty"]), b.get("kind", "market"),
                     _num(b.get("price")), _num(b.get("stop")), _num(b.get("target1")), _num(b.get("target2")),
                     b.get("emotion", ""), b.get("reason", ""), b.get("pick_id"), bool(b.get("auto", True)))


def _num(x):
    try:
        return float(x) if x not in (None, "") else None
    except (TypeError, ValueError):
        return None


def p_cancel(b):
    sim.cancel(int(b["id"]))
    return {"ok": True}


def p_exit(b):
    sim.set_exit(b.get("acct", "me"), market.norm_code(b["code"]), _num(b.get("stop")), _num(b.get("target1")),
                 _num(b.get("target2")), b.get("auto"))
    return {"ok": True}


def p_reset(b):
    sim.reset(b.get("acct", "me"))
    return {"ok": True}


def p_learn(_b):
    return learn.daily()


def p_boot(_b):
    if learn.BOOT["running"]:
        return {"started": False, "message": "冷启动正在进行中"}
    threading.Thread(target=learn.bootstrap, daemon=True).start()
    return {"started": True}


def p_settings(b):
    b = dict(b)
    n = b.get("notify")
    if isinstance(n, dict):  # token 改存本机保险箱
        vault.set_many({k: n.pop(k) for k in ("pushplus_token", "serverchan_key") if n.get(k) and n.get(k) != "已设置"})
    b.pop("security", None)
    n = b.get("notify")
    if isinstance(n, dict):  # 掩码值不覆盖真实 token
        for k in ("pushplus_token", "serverchan_key"):
            if n.get(k) == "已设置":
                n.pop(k)
    b.pop("emotions", None)
    save_settings(b)
    return public_settings()


def p_watch(b):
    s = load_settings()
    wl = [market.norm_code(c) for c in s["watchlist"]]
    code = market.norm_code(b["code"])
    if b.get("op") == "remove":
        wl = [c for c in wl if c != code]
    elif b.get("op") == "move" and code in wl:  # 调整顺序：dir=-1 上移，1 下移，"top" 置顶
        i = wl.index(code)
        d = b.get("dir", -1)
        j = 0 if d == "top" else max(0, min(len(wl) - 1, i + int(d)))
        wl.insert(j, wl.pop(i))
    elif code not in wl:
        wl.append(code)
    save_settings({"watchlist": wl})
    return {"watchlist": wl}


def p_notify_test(_b):
    return notify.send("A量化 · 推送测试", "如果你在微信看到这条消息，说明推送配置成功。", force=True)


def p_publish(b):
    if publisher.STATE["running"]:
        return {"started": False, "message": "正在发布"}
    publisher.publish_async(full=bool(b.get("full", True)), force=True)
    return {"started": True}


def p_events_read(_b):
    with db.tx() as c:
        c.execute("UPDATE events SET read=1")
    return {"ok": True}


POST = {"/api/picks/run": p_pick_run, "/api/sim/order": p_order, "/api/sim/cancel": p_cancel,
        "/api/sim/exit": p_exit, "/api/sim/reset": p_reset, "/api/learn/run": p_learn, "/api/learn/bootstrap": p_boot, "/api/settings": p_settings,
        "/api/watchlist": p_watch, "/api/notify/test": p_notify_test, "/api/events/read": p_events_read, "/api/publish": p_publish,
        "/api/me/profile": p_profile, "/api/me/vault": p_vault, "/api/me/password": p_password,
        "/api/me/revoke": p_revoke, "/api/me/token": p_api_token, "/api/me/backup": p_backup,
        "/api/me/restore": p_restore, "/api/me/security": p_security,
        "/api/ai/config": p_ai_config, "/api/ai/test": lambda b: ai.test_connection(), "/api/ai/ask": p_ai_ask,
        "/api/ai/review": lambda b: ai.daily_review(force=True), "/api/ai/suggest": lambda b: ai.suggest_iteration(),
        "/api/ai/adopt": lambda b: ai.adopt_suggestion(),
        "/api/ai/chat": lambda b: ai.assistant(b.get("messages") or [], b.get("page", ""), b.get("code")),
        "/api/ai/check": lambda b: ai.order_check(b["code"], b.get("side", "buy"), int(b.get("qty") or 100),
                                                  float(b["price"]) if b.get("price") else None),
        "/api/ai/brief": lambda b: ai.morning_brief(force=True),
        "/api/ai/holdings": lambda b: ai.holdings_check(),
        "/api/ai/run": lambda b: ai.explain_run(int(b["id"]), force=True),
        "/api/ai/trade": lambda b: ai.trade_review(int(b["id"]), force=bool(b.get("force"))),
        "/api/me/dbsnap": lambda b: backup.db_snapshot(),
        "/api/watch/run": lambda b: signals.run_watch(b.get("phase")),
        "/api/ai/mode": lambda b: save_settings({"pick": {"ai_mode": "high" if b.get("mode") == "high" else "low"}})["pick"]["ai_mode"],
        "/api/hf/scan": lambda b: hf.quick_scan(force=True),
        "/api/hf/step": lambda b: hf.step(),
        "/api/watch/plan": lambda b: signals.plan(b["code"]),
        "/api/ai/pick": lambda b: ai.pick_review(int(b["id"])),
        "/api/remote/enable": lambda b: tunnel.set_enabled(bool(b.get("on"))),
        "/api/remote/download": lambda b: tunnel.download(),
        "/api/remote/relay": lambda b: tunnel.set_relay(b.get("url", ""), b.get("secret", "")),
        "/api/remote/restart": lambda b: (tunnel.stop(), tunnel.start())[1],
        "/api/sectors/refresh": lambda b: (sectors.refresh_map_async(force=True), {"started": True})[1]}


SENSITIVE = {"/api/me/vault", "/api/me/password", "/api/me/restore", "/api/me/token", "/api/me/revoke",
             "/api/me/security", "/api/remote/enable", "/api/remote/download", "/api/remote/relay"}


class Handler(BaseHTTPRequestHandler):
    server_version = "AQ5/" + VERSION

    def log_message(self, *a):
        pass

    def _cors(self):
        """只允许设置里的来源（默认 GitHub Pages）跨域调用，带 Token 访问。"""
        o = self.headers.get("Origin") or ""
        if o and o in load_settings().get("remote", {}).get("origins", []):
            self.send_header("Access-Control-Allow-Origin", o)
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Max-Age", "600")
            self.send_header("Vary", "Origin")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send(self, code, body: bytes, ctype="application/json; charset=utf-8", cache=False):
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=300" if cache else "no-store")
        try:
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass  # 浏览器提前关闭了连接，忽略

    def _json(self, obj, code=200, cookie=None):
        body = json.dumps(clean(obj), ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        try:
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass

    # ---------------------------------------------------------------- 登录校验
    def _token(self):
        h = self.headers.get("Authorization") or ""
        if h.startswith("Bearer "):
            return h[7:].strip()
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "aq5s":
                return v
        return None

    def _is_local(self):
        # 经过 Cloudflare 隧道的请求虽然来自 127.0.0.1，但带有 Cf-/转发头：一律按外网处理
        if any(self.headers.get(h) for h in ("Cf-Ray", "Cf-Connecting-Ip", "X-Forwarded-For", "Cf-Visitor")):
            return False
        return self.client_address[0] in auth.LOCAL

    def _authorized(self) -> bool:
        REQ.local = self._is_local()
        if REQ.local and load_settings().get("security", {}).get("local_no_login", True):
            return True
        if not auth.has_user():
            return REQ.local
        return auth.check(self._token()) is not None

    def _auth_route(self, path, body) -> bool:
        ip = self.headers.get("Cf-Connecting-Ip") or self.client_address[0]  # 隧道过来的按真实 IP 计失败次数
        ua = self.headers.get("User-Agent") or ""
        try:
            if path == "/api/auth/me":
                sess = auth.check(self._token())
                self._json({"has_user": auth.has_user(), "local": self._is_local(), "authorized": self._authorized(),
                            "username_hint": ((auth.user() or {}).get("username") or "")[:1] + "…" if auth.has_user() else None,
                            "user": (auth.user() or {}).get("username") if (sess or self._authorized()) else None})
            elif path == "/api/auth/setup":
                if not self._is_local():
                    return self._json({"error": "首次创建账号只能在电脑本机操作"}, 403) or True
                auth.setup(body.get("username", ""), body.get("password", ""))
                tok = auth.login(body.get("username", ""), body.get("password", ""), ip, ua)
                self._json({"ok": True}, cookie=self._cookie(tok))
            elif path == "/api/auth/login":
                tok = auth.login(body.get("username", ""), body.get("password", ""), ip, ua)
                self._json({"ok": True}, cookie=self._cookie(tok))
            elif path == "/api/auth/reset":
                if not self._is_local():
                    return self._json({"error": "忘记密码只能在电脑本机重置"}, 403) or True
                name = auth.reset_password(body.get("password", ""))
                tok = auth.login(name, body.get("password", ""), ip, ua)
                self._json({"ok": True, "username": name}, cookie=self._cookie(tok))
            elif path == "/api/auth/logout":
                auth.logout(self._token())
                self._json({"ok": True}, cookie="aq5s=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")
            else:
                return False
        except ValueError as e:
            self._json({"error": str(e)}, 400)
        return True

    @staticmethod
    def _cookie(tok):
        return f"aq5s={tok}; Path=/; Max-Age={auth.SESSION_DAYS * 86400}; HttpOnly; SameSite=Lax"

    def _handle(self, table, arg):
        path = urlparse(self.path).path
        fn = table.get(path)
        if not fn:
            return False
        try:
            self._json(fn(arg))
        except (ValueError, KeyError) as e:
            self._json({"error": str(e)}, 400)
        except RuntimeError as e:
            self._json({"error": "行情源暂时不可用：" + str(e)}, 502)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)
        return True

    def do_GET(self):
        u = urlparse(self.path)
        if u.path.startswith("/api/"):
            if u.path.startswith("/api/auth/") and self._auth_route(u.path, {}):
                return
            if not self._authorized():
                return self._json({"error": "需要登录", "need_login": True}, 401)
            if not self._handle(GET, parse_qs(u.query)):
                self._json({"error": "not found"}, 404)
            return
        rel = u.path.lstrip("/") or "index.html"
        f = (WEB / rel).resolve()
        if not str(f).startswith(str(WEB.resolve())) or not f.is_file():
            f = WEB / "index.html"
        ctype = mimetypes.guess_type(str(f))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self._send(200, f.read_bytes(), ctype, cache=f.suffix in (".png", ".svg", ".woff2"))

    def do_POST(self):
        if urlparse(self.path).path == "/api/admin/restart":
            if self.client_address[0] not in ("127.0.0.1", "::1"):
                return self._json({"error": "只允许本机重启"}, 403)
            self._json({"ok": True, "message": "服务将在 1 秒后重启"})
            threading.Thread(target=_restart, daemon=True).start()
            return
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            body = {}
        path = urlparse(self.path).path
        if path.startswith("/api/auth/") and self._auth_route(path, body):
            return
        if not self._authorized():
            return self._json({"error": "需要登录", "need_login": True}, 401)
        if path in SENSITIVE and not self._is_local():
            sess = auth.check(self._token())
            if not sess or sess.get("kind") == "api":
                return self._json({"error": "这个操作只能在电脑本机或网页登录后进行（手机远程 Token 无权限）"}, 403)
        if not self._handle(POST, body):
            self._json({"error": "not found"}, 404)


def _restart():
    """更新代码后重启自身（新进程接管端口）。"""
    import os
    import subprocess
    import time as _t
    _t.sleep(1)
    try:
        tunnel.stop()  # 子进程不会随引擎退出，先关掉隧道
    except Exception:  # noqa: BLE001
        pass
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    log = open(os.path.join(str(DATA), "server-restart.log"), "a", encoding="utf-8")
    subprocess.Popen([sys.executable, "-c", "import time;time.sleep(2);from aq5.server import main;main()"],
                     cwd=str(WEB.parent), stdout=log, stderr=log, creationflags=flags)
    os._exit(0)


def _migrate_secrets():
    """旧版把推送 token 明文写在 settings.json，迁移到本机保险箱。"""
    s = load_settings()
    n = s.get("notify", {})
    moved = {k: n[k] for k in ("pushplus_token", "serverchan_key") if n.get(k)}
    if moved:
        vault.set_many(moved)
        save_settings({"notify": {k: "" for k in moved}})


def main():
    db.init()
    auth.init()
    ai.init()
    sectors.init()
    signals.init()
    marketstate.init()
    sectors.refresh_map_async()
    tunnel.cleanup_stale()
    hf.start()
    tunnel.watchdog()
    _migrate_secrets()
    s0 = load_settings()
    if 0 < s0.get("settings_rev", 0) < 3 or s0.get("settings_rev", 0) == 2:
        save_settings({"settings_rev": 3, "pick": {"ai_mode": "high"}})
    if s0.get("settings_rev", 0) < 2:  # 一次性：模拟资金提到 100 万、AI 自动盘更积极
        save_settings({"settings_rev": 2, "init_cash": max(float(s0.get("init_cash", 0)), 1000000.0),
                       "pick": {"ai_max_positions": max(int(s0["pick"].get("ai_max_positions", 0)), 12),
                                "ai_position_pct": 0.12}})
    sim.ensure_accounts()
    sim.migrate_capital()
    market.ensure_universe_mock()
    s = load_settings()
    port = int(sys.argv[1]) if len(sys.argv) > 1 else s["port"]
    try:
        httpd = ThreadingHTTPServer((s["host"], port), Handler)
    except OSError as e:
        print(f"端口 {port} 已被占用（A量化可能已经在运行）：{e}", flush=True)
        return
    httpd.daemon_threads = True
    scheduler.start()
    if not db.q1("SELECT 1 FROM samples LIMIT 1"):
        def _boot():
            import time as _t
            _t.sleep(20)
            try:
                learn.bootstrap()
            except Exception as e:  # noqa: BLE001
                db.event("learn", "历史冷启动失败", str(e), "warn")
        threading.Thread(target=_boot, daemon=True).start()
    print(f"A量化 5.0 已启动  电脑: http://127.0.0.1:{port}   手机(同一WiFi): http://{lan_ip()}:{port}"
          + ("   [模拟行情模式]" if MOCK else ""), flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
