"""后台调度：盘中撮合、9:26 竞价选股、14:45 尾盘选股、收盘记账与自我学习、异动提醒。"""
from __future__ import annotations

import threading
import time
import traceback

from . import ai, backup, db, learn, market, picker, publisher, sim
from .config import load_settings, now, today

STATUS = {"started": None, "last_tick": None, "jobs": {}, "errors": []}
_done: set[str] = set()
_alerted: set[str] = set()


def _once(key: str, fn) -> None:
    k = today() + ":" + key
    if k in _done:
        return
    _done.add(k)
    try:
        STATUS["jobs"][key] = {"time": now().strftime("%H:%M:%S"), "status": "running"}
        fn()
        STATUS["jobs"][key]["status"] = "done"
    except Exception as e:  # noqa: BLE001
        STATUS["jobs"][key]["status"] = "error: " + str(e)[:120]
        STATUS["errors"] = (STATUS["errors"] + [f"{now():%H:%M} {key}: {e}"])[-20:]
        db.event("system", f"任务 {key} 出错", traceback.format_exc()[-600:], "error")


def _ran(session: str) -> bool:
    return bool(db.q1("SELECT 1 FROM pick_runs WHERE date=? AND session=?", (today(), session)))


def _hm(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 100 + int(m)


def alerts() -> None:
    poss = db.q("SELECT account, code, name, stop, target1 FROM positions")
    wl = load_settings()["watchlist"]
    codes = list({p["code"] for p in poss} | {market.norm_code(c) for c in wl})
    if not codes:
        return
    qs = market.quotes(codes)
    for p in poss:
        qd = qs.get(p["code"]) or {}
        px = qd.get("price")
        if px and p["stop"] and px <= p["stop"] * 1.01:
            k = f"{today()}:{p['account']}:{p['code']}:stop"
            if k not in _alerted:
                _alerted.add(k)
                db.event("alert", f"{p['name']} 接近止损价 {p['stop']:.2f}", f"现价 {px:.2f}", "warn")
    for c in codes:
        qd = qs.get(c) or {}
        pct = qd.get("pct")
        if pct is None:
            continue
        for lv in (5, 7, 9.5):
            if abs(pct) >= lv:
                k = f"{today()}:{c}:{lv}:{pct > 0}"
                if k not in _alerted:
                    _alerted.add(k)
                    db.event("alert", f"{qd.get('name')} {'大涨' if pct > 0 else '大跌'} {pct:+.2f}%",
                             f"现价 {qd.get('price')}，量比 {qd.get('vr')}", "good" if pct > 0 else "warn")


def loop() -> None:
    STATUS["started"] = now().strftime("%Y-%m-%d %H:%M:%S")
    last_alert = 0.0
    last_pub = 0.0
    while True:
        try:
            STATUS["last_tick"] = now().strftime("%H:%M:%S")
            t = now()
            hm = t.hour * 100 + t.minute
            s = load_settings()
            times = s["pick"]["times"]
            if t.weekday() < 5 and market.is_trading_day():
                a, c = _hm(times[0]), _hm(times[1])
                if a <= hm < a + 4 and not _ran("auction"):
                    _once("auction", lambda: picker.run("auction"))
                if c <= hm < c + 6 and not _ran("close"):
                    _once("close", lambda: picker.run("close"))
                if market.market_phase() == "trading":
                    sim.tick()
                    if time.time() - last_alert > 30:
                        last_alert = time.time()
                        alerts()
                if hm >= 1505:
                    _once("equity", sim.record_equity)
                if hm >= 1530:
                    _once("learn", learn.daily)
                pub = s["publish"]
                if pub.get("enabled"):
                    if market.market_phase() in ("trading", "noon") and time.time() - last_pub > pub["interval_min"] * 60:
                        last_pub = time.time()
                        publisher.publish_async(full=False)
                    if hm >= 1545:
                        _once("publish", lambda: publisher.publish(full=True))
                if hm >= 1540 and ai.ready() and ai.cfg()["auto_review"]:
                    _once("ai_review", ai.daily_review)
                bk = s.get("backup", {})
                if bk.get("enabled") and hm >= _hm(bk.get("time", "15:50")):
                    _once("backup", lambda: backup.backup(push=bool(bk.get("remote"))))
        except Exception as e:  # noqa: BLE001
            STATUS["errors"] = (STATUS["errors"] + [f"{now():%H:%M:%S} loop: {e}"])[-20:]
        time.sleep(5)


def start() -> None:
    threading.Thread(target=loop, name="aq5-scheduler", daemon=True).start()
