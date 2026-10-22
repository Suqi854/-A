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

from . import analysis, backtest, db, learn, market, notify, picker, publisher, scheduler, sim
from .config import MOCK, WEB, load_settings, now, public_settings, save_settings
from .strategies import DESC, NAMES

VERSION = "5.0.0"


def lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


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
            "breadth": br, "regime": reg, "top": top, "bottom": bottom, "inflow": inflow,
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
    rows = market.kline_with_live(code, n)
    return {"code": market.norm_code(code), "rows": rows}


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
    out["strategies"] = [{"id": k, "name": n, "desc": d} for k, n, _, d in STRATEGIES]
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


GET = {"/api/overview": api_overview, "/api/quotes": api_quotes, "/api/kline": api_kline,
       "/api/intraday": api_intraday, "/api/analyze": api_analyze, "/api/search": api_search,
       "/api/picks/runs": api_runs, "/api/picks/candidates": api_candidates, "/api/picks/state": api_pick_state,
       "/api/sim": api_sim, "/api/learn": api_learn, "/api/backtest": api_backtest, "/api/settings": api_settings,
       "/api/events": api_events, "/api/strategies": api_strategies}


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
        "/api/watchlist": p_watch, "/api/notify/test": p_notify_test, "/api/events/read": p_events_read, "/api/publish": p_publish}


class Handler(BaseHTTPRequestHandler):
    server_version = "AQ5/" + VERSION

    def log_message(self, *a):
        pass

    def _send(self, code, body: bytes, ctype="application/json; charset=utf-8", cache=False):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=300" if cache else "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(clean(obj), ensure_ascii=False).encode("utf-8"))

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
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            body = {}
        if not self._handle(POST, body):
            self._json({"error": "not found"}, 404)


def main():
    db.init()
    sim.ensure_accounts()
    market.ensure_universe_mock()
    s = load_settings()
    port = int(sys.argv[1]) if len(sys.argv) > 1 else s["port"]
    httpd = ThreadingHTTPServer((s["host"], port), Handler)
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
