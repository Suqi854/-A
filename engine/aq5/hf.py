"""AI 自动盘 · 高频模式（模拟的日内量化交易，T+0 + 融资融券）。

和真实量化软件的区别（如实说明）：
  - 决策和撮合在本机内存里完成，单次决策→成交是毫秒级（日志里记录实际耗时）；
  - 但免费行情源大约 2~3 秒刷新一次，所以“看到的价格”最快也是秒级，没有逐笔/Level-2 数据。

每 2 秒一个 tick：
  1) 行情：一次批量请求取候选池 + 持仓的实时报价（含五档、内外盘、均价）
  2) 特征：VWAP 偏离、五档委比、30 秒/2 分钟/5 分钟动量（本地 tick 缓存计算）、主动买卖差（外盘−内盘增量）
  3) 做多：评分≥60，价格>VWAP，委比>+0.15，2分钟动量>+0.12%，30秒动量≥0，涨幅 0.3%~7.5%
     做空（融券）：评分≤42，价格<VWAP，委比<−0.15，2分钟动量<−0.12%，30秒动量≤0，涨幅 −7.5%~−0.3%
  4) 出场：止盈 +1.5%、止损 −1.0%、浮盈≥0.8% 后回撤 0.6% 移动止盈、信号反转（动量和委比同时反向）
  5) 风控：单票 5% 净资产、总敞口 ≤ 杠杆上限、每天 ≤ 300 笔、同一股票平仓后冷却 5 分钟、
     大盘“防守”时不开多仓、14:56 日内仓位全部平掉（可关）
候选池每 10 分钟后台刷新：全市场粗筛 → 精评，强的做多候选、弱的做空候选。
"""
from __future__ import annotations

import threading
import time
from collections import deque

from . import db, market
from .config import load_settings, now, today

STATE = {"last_scan": None, "last_tick": None, "ticks": 0, "candidates": [], "shorts": [], "log": [], "latency_ms": None,
         "orders_today": 0, "scanning": False}
_buf: dict[str, deque] = {}
_best: dict[str, float] = {}
_cool: dict[str, float] = {}
_scan = {"t": 0.0, "long": [], "short": []}
_lock = threading.Lock()


def cfg() -> dict:
    c = {"tick_sec": 2, "pos_pct": 0.05, "take": 1.5, "stop": 1.0, "trail_on": 0.8, "trail": 0.6, "max_orders": 300,
         "max_pos": 20, "flat_eod": True, "long_score": 60, "short_score": 42, "imb": 0.15, "mom": 0.12}
    c.update(load_settings().get("hf", {}))
    return c


def mode() -> str:
    return load_settings()["pick"].get("ai_mode", "low")


def _log(msg: str) -> None:
    STATE["log"] = ([now().strftime("%H:%M:%S.%f")[:12] + " " + msg] + STATE["log"])[:120]


# ----------------------------------------------------------------------------- 候选池
def scan() -> None:
    if STATE["scanning"]:
        return
    STATE["scanning"] = True
    try:
        from . import analysis, picker
        from .factors import current_weights
        from .strategies import strategy_weights
        s = load_settings()
        snap = market.snapshot()
        br = market.breadth(snap)
        liquid = [r for r in snap if picker._base_filter(r, "close", s["pick"]) and (r.get("amount") or 0) >= 3e8]
        strong = sorted(liquid, key=lambda r: -picker._prescore(r, "intraday"))[:30]
        weak = sorted([r for r in liquid if (r.get("pct") or 0) < 0 and (r.get("mcap") or 0) >= 100],
                      key=lambda r: (r.get("pct") or 0))[:20]
        _, w = current_weights()
        sw = strategy_weights()

        def ev(r):
            rows = market.kline(r["code"], 130, history=True)
            if len(rows) < 60:
                return None
            if rows[-1]["date"] == today():
                rows = rows[:-1]
            rows = rows + [{"date": today(), "open": r.get("open") or r["price"], "close": r["price"],
                            "high": r.get("high") or r["price"], "low": r.get("low") or r["price"], "vol": r.get("vol") or 0}]
            e = analysis.evaluate(r["code"], rows, r, "intraday", br["median_pct"], None, w, sw, s["risk"])
            return {"code": r["code"], "name": r.get("name"), "score": e["score"], "pct": r.get("pct"), "plan": e["plan"]}
        lo, sh = [], []
        for r in strong:
            try:
                x = ev(r)
                if x and x["score"] >= cfg()["long_score"]:
                    lo.append(x)
            except (RuntimeError, ValueError, ZeroDivisionError, TypeError):
                pass
        for r in weak:
            try:
                x = ev(r)
                if x and x["score"] <= cfg()["short_score"]:
                    sh.append(x)
            except (RuntimeError, ValueError, ZeroDivisionError, TypeError):
                pass
        lo.sort(key=lambda x: -x["score"])
        sh.sort(key=lambda x: x["score"])
        _scan.update(t=time.time(), long=lo[:15], short=sh[:10])
        STATE.update(last_scan=now().strftime("%H:%M:%S"),
                     candidates=[{k: x[k] for k in ("code", "name", "score", "pct")} for x in lo[:15]],
                     shorts=[{k: x[k] for k in ("code", "name", "score", "pct")} for x in sh[:10]])
        _log(f"候选池刷新：做多 {len(lo[:15])} 只、做空 {len(sh[:10])} 只")
    finally:
        STATE["scanning"] = False


# ----------------------------------------------------------------------------- 特征
def _feat(code: str, q: dict) -> dict:
    t = time.time()
    b = _buf.setdefault(code, deque(maxlen=400))
    b.append((t, q["price"], q.get("outer") or 0, q.get("inner") or 0))

    def back(sec):
        for x in b:
            if t - x[0] <= sec:
                return x
        return b[0]
    p = q["price"]
    m30, m120, m300 = ((p / back(s)[1] - 1) * 100 for s in (30, 120, 300))
    o60 = back(60)
    flow = (q.get("outer") or 0) - o60[2] - ((q.get("inner") or 0) - o60[3])
    sb = sum(x[1] or 0 for x in q.get("bids") or [])
    sa = sum(x[1] or 0 for x in q.get("asks") or [])
    imb = (sb - sa) / (sb + sa) if sb + sa else 0.0
    vwap = q.get("avg") or p
    return {"p": p, "vwap": vwap, "dv": (p / vwap - 1) * 100 if vwap else 0, "m30": m30, "m120": m120, "m300": m300,
            "imb": imb, "flow": flow, "pct": q.get("pct") or 0, "n": len(b)}


# ----------------------------------------------------------------------------- 主循环
def tick() -> dict:
    from . import sim
    c = cfg()
    if time.time() - _scan["t"] > 600 and not STATE["scanning"]:
        threading.Thread(target=scan, daemon=True, name="aq5-hfscan").start()
    poss = {p["code"]: p for p in db.q("SELECT * FROM positions WHERE account='ai'")}
    longs, shorts = _scan["long"], _scan["short"]
    codes = list({*poss, *(x["code"] for x in longs), *(x["code"] for x in shorts)})
    if not codes:
        return {}
    qs = market.quotes_fast(codes)
    feats = {k: _feat(k, q) for k, q in qs.items() if q.get("price")}
    nm = {x["code"]: (x["name"], "多", x["score"]) for x in longs} | {x["code"]: (x["name"], "空", x["score"]) for x in shorts}
    STATE["watch"] = [{"code": k, "name": nm.get(k, (qs[k].get("name"), "持", None))[0], "dir": nm.get(k, (None, "持"))[1],
                       "score": nm.get(k, (None, None, None))[2], **{kk: round(v, 3) for kk, v in f.items() if kk != "n"}, "n": f["n"]}
                      for k, f in feats.items()][:40]
    STATE["ticks"] += 1
    STATE["last_tick"] = now().strftime("%H:%M:%S")
    n_today = (db.q1("SELECT COUNT(*) AS n FROM orders WHERE account='ai' AND created LIKE ?", (today() + "%",)) or {}).get("n", 0)
    STATE["orders_today"] = n_today
    hm = now().hour * 100 + now().minute

    def act(code, side, qty, why):
        t0 = time.perf_counter()
        try:
            o = sim.place("ai", code, side, qty, reason=why, emotion="跟随系统", qd=qs.get(code))
            ms = (time.perf_counter() - t0) * 1000
            STATE["latency_ms"] = round(ms, 2)
            _log(f"{'买' if side == 'buy' else '卖'} {o.get('name')} {qty}股 @ {o.get('fill_price')}（{ms:.1f}ms）{why}")
            return True
        except ValueError as e:
            _log(f"下单失败 {code}: {e}")
            return False

    # —— 出场 ——
    for code, p in poss.items():
        f = feats.get(code)
        if not f or f["n"] < 3:
            continue
        if not str(p.get("source") or "").startswith("高频") and p["qty"] > 0:
            continue   # 竞价/尾盘选股买入的波段仓位由止损止盈规则管理，高频不动它
        q = p["qty"]
        r = (f["p"] / p["cost"] - 1) * 100 * (1 if q > 0 else -1)
        _best[code] = max(_best.get(code, r), r)
        why = None
        if c["flat_eod"] and hm >= 1456:
            why = "日内平仓（14:56）"
        elif r >= c["take"]:
            why = f"止盈 {r:+.2f}%"
        elif r <= -c["stop"]:
            why = f"止损 {r:+.2f}%"
        elif _best[code] >= c["trail_on"] and r <= _best[code] - c["trail"]:
            why = f"移动止盈：最高 {_best[code]:+.2f}% 回撤到 {r:+.2f}%"
        elif q > 0 and f["m120"] < -0.25 and f["imb"] < -0.3 and f["p"] < f["vwap"]:
            why = f"信号反转：2分钟 {f['m120']:+.2f}%、委比 {f['imb']:+.2f}、跌破VWAP"
        elif q < 0 and f["m120"] > 0.25 and f["imb"] > 0.3 and f["p"] > f["vwap"]:
            why = f"信号反转：2分钟 {f['m120']:+.2f}%、委比 {f['imb']:+.2f}、站上VWAP"
        if why and (q < 0 or sim.available("ai", code) > 0):
            if act(code, "buy" if q < 0 else "sell", abs(q), "高频·" + ("平空 " if q < 0 else "平多 ") + why):
                _best.pop(code, None)
                _cool[code] = time.time()
    if c["flat_eod"] and hm >= 1456:
        return {"flat": True}
    if n_today >= c["max_orders"] or hm >= 1450:
        return {}
    # —— 进场 ——
    try:
        from . import marketstate
        ms = marketstate.state()
    except Exception:  # noqa: BLE001
        ms = {}
    ex = sim.exposure("ai")
    eq = ex["equity"]
    held = set(poss)
    opened = 0

    def size(px):
        return int(eq * c["pos_pct"] / (px * 1.003) / 100) * 100

    if not ms.get("risk_off"):
        for x in longs:
            if opened >= 2 or len(held) >= c["max_pos"]:
                break
            f = feats.get(x["code"])
            if not f or f["n"] < 15 or x["code"] in held or time.time() - _cool.get(x["code"], 0) < 300:
                continue
            if f["p"] > f["vwap"] and f["imb"] > c["imb"] and f["m120"] > c["mom"] and f["m30"] >= 0 and 0.3 <= f["pct"] <= 7.5:
                qty = size(f["p"])
                if qty >= 100 and act(x["code"], "buy", qty, f"高频·做多：评分 {x['score']:.0f}，VWAP偏离 {f['dv']:+.2f}%，委比 {f['imb']:+.2f}，"
                                                             f"2分钟 {f['m120']:+.2f}%，30秒 {f['m30']:+.2f}%，主动净买 {f['flow']:+.0f}手"):
                    held.add(x["code"])
                    opened += 1
    if sim.ai_rules()["short"] and ms.get("stance") != "顺势做多":
        for x in shorts:
            if opened >= 2 or len(held) >= c["max_pos"]:
                break
            f = feats.get(x["code"])
            if not f or f["n"] < 15 or x["code"] in held or time.time() - _cool.get(x["code"], 0) < 300:
                continue
            if f["p"] < f["vwap"] and f["imb"] < -c["imb"] and f["m120"] < -c["mom"] and f["m30"] <= 0 and -7.5 <= f["pct"] <= -0.3:
                qty = size(f["p"])
                if qty >= 100 and act(x["code"], "sell", qty, f"高频·融券做空：评分 {x['score']:.0f}，VWAP偏离 {f['dv']:+.2f}%，委比 {f['imb']:+.2f}，"
                                                              f"2分钟 {f['m120']:+.2f}%，30秒 {f['m30']:+.2f}%"):
                    held.add(x["code"])
                    opened += 1
    return {"opened": opened}


def _loop():
    while True:
        try:
            if mode() == "high" and market.market_phase() == "trading":
                with _lock:
                    tick()
                from . import sim
                sim.check_maintenance()
        except Exception as e:  # noqa: BLE001
            _log(f"出错：{e}")
        time.sleep(max(1, cfg()["tick_sec"]))


def start() -> None:
    threading.Thread(target=_loop, daemon=True, name="aq5-hf").start()


def step() -> dict:
    """手动跑一个 tick（调试用）。"""
    if market.market_phase() != "trading":
        return {"skipped": True}
    with _lock:
        return tick()


def quick_scan(force: bool = False):
    if force or not _scan["long"]:
        scan()
    return _scan["long"]


def status() -> dict:
    from . import sim
    return {"mode": mode(), "cfg": cfg(), "rules": sim.ai_rules(), **STATE}
