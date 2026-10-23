"""AI 自动盘 · 高频模式（日内量化）。

低频模式：只在 9:26 竞价选股、14:45 尾盘选股时按分级买入，持仓靠止损/止盈/时间止损和做T管理。
高频模式：交易时间每 3 分钟运行一轮——
  1) 快速扫描（每 15 分钟刷新）：全市场快照粗筛 → 前 40 只做多因子+策略精评 → 取前 12 只为候选池
  2) 进场：候选评分 ≥ 60，且日内确认：价格 > VWAP、5 分钟 RSI 在 45~75、当日涨幅 0.5%~7%
     单票仓位 = 总资产 × 6% × (评分/70)，每轮最多买 3 只，最多同时持 15 只
  3) 出场（只能卖昨天以前买的，A股 T+1）：
     移动止盈：浮盈中且从日内最高回落 ≥ 2% 并跌破 VWAP → 全部卖出
     动能转弱：跌破 VWAP、5 分钟 RSI < 35 且浮亏 > 1.5% → 卖一半
     做T：由 signals.scan_t 负责（高抛 1/3、低吸买回）
  4) 风控：每天最多 40 笔委托；大盘异动下跌时暂停买入
"""
from __future__ import annotations

import threading
import time

from . import db, market
from .config import load_settings, now, today

STATE = {"last_scan": None, "last_step": None, "candidates": [], "log": [], "running": False}
_scan = {"t": 0.0, "data": []}
_lock = threading.Lock()


def mode() -> str:
    return load_settings()["pick"].get("ai_mode", "low")


def _log(msg: str) -> None:
    STATE["log"] = ([now().strftime("%H:%M:%S") + " " + msg] + STATE["log"])[:60]


def quick_scan(force: bool = False) -> list[dict]:
    if not force and time.time() - _scan["t"] < 900 and _scan["data"]:
        return _scan["data"]
    from . import analysis, picker
    from .factors import current_weights
    from .strategies import strategy_weights
    s = load_settings()
    cfg = s["pick"]
    snap = market.snapshot()
    br = market.breadth(snap)
    pool = [r for r in snap if picker._base_filter(r, "close", cfg)]
    pool.sort(key=lambda r: -picker._prescore(r, "intraday"))
    _, w = current_weights()
    sw = strategy_weights()
    out = []
    for r in pool[:40]:
        try:
            rows = market.kline(r["code"], 130, history=True)
            if len(rows) < 60:
                continue
            if rows[-1]["date"] == today():
                rows = rows[:-1]
            rows = rows + [{"date": today(), "open": r.get("open") or r["price"], "close": r["price"],
                            "high": r.get("high") or r["price"], "low": r.get("low") or r["price"], "vol": r.get("vol") or 0}]
            ev = analysis.evaluate(r["code"], rows, r, "intraday", br["median_pct"], None, w, sw, s["risk"])
        except (RuntimeError, ValueError, ZeroDivisionError, TypeError):
            continue
        out.append({"code": r["code"], "name": r.get("name"), "score": ev["score"], "price": r["price"], "pct": r.get("pct"),
                    "plan": ev["plan"], "signals": ev["signals"][:2], "prob": ev["prob"]})
    out.sort(key=lambda x: -x["score"])
    _scan.update(t=time.time(), data=out[:12])
    STATE.update(last_scan=now().strftime("%H:%M:%S"), candidates=[{k: x[k] for k in ("code", "name", "score", "price", "pct")}
                                                                   for x in out[:12]])
    return _scan["data"]


def step() -> dict:
    """高频一轮。"""
    if mode() != "high" or market.market_phase() != "trading":
        return {"skipped": True}
    with _lock:
        if STATE["running"]:
            return {"skipped": "running"}
        STATE["running"] = True
    try:
        return _step()
    finally:
        STATE["running"] = False
        STATE["last_step"] = now().strftime("%H:%M:%S")


def _step() -> dict:
    from . import signals, sim
    n_today = (db.q1("SELECT COUNT(*) AS n FROM orders WHERE account='ai' AND created LIKE ?", (today() + "%",)) or {}).get("n", 0)
    if n_today >= 40:
        return {"skipped": "今日委托已达 40 笔上限"}
    v = sim.view("ai", light=True)
    held = {p["code"]: p for p in v["positions"]}
    pend = {o["code"] for o in db.q("SELECT code FROM orders WHERE account='ai' AND status='pending'")}
    acted = []
    # —— 出场 ——
    for code, p in held.items():
        if p["avail"] <= 0 or code in pend:
            continue
        try:
            lv = signals.levels(code)
        except Exception:  # noqa: BLE001
            continue
        q = lv["quote"] or {}
        px, hi, vwap, rsi5 = lv["price"], q.get("high"), lv["vwap"], lv["rsi5"]
        if not (px and vwap):
            continue
        pnl = (px / p["cost"] - 1) * 100
        try:
            if pnl > 0 and hi and px <= hi * 0.98 and px < vwap:
                sim.place("ai", code, "sell", p["avail"], reason=f"高频·移动止盈：从日内高点 {hi:.2f} 回落 {(1 - px / hi) * 100:.1f}% 且跌破均价 {vwap:.2f}（浮盈 {pnl:+.1f}%）",
                          emotion="跟随系统")
                acted.append(f"卖 {p['name']}（移动止盈）")
            elif px < vwap and rsi5 is not None and rsi5 < 35 and pnl < -1.5:
                q2 = max(100, int(p["avail"] / 2 / 100) * 100) if p["avail"] >= 200 else p["avail"]
                sim.place("ai", code, "sell", q2, reason=f"高频·动能转弱：跌破均价 {vwap:.2f}、5分钟RSI {rsi5:.0f}、浮亏 {pnl:.1f}%，先减一半",
                          emotion="跟随系统")
                acted.append(f"减 {p['name']}（动能转弱）")
        except ValueError as e:
            _log(f"卖出失败 {p['name']}: {e}")
    # —— 进场 ——
    try:
        from . import marketstate
        ms = marketstate.state()
        if ms.get("risk_off"):
            _log("大盘风险：" + ms.get("label", "") + "，本轮暂停买入")
            return {"acted": acted, "paused": True}
    except Exception:  # noqa: BLE001
        pass
    cands = quick_scan()
    buys = 0
    for c in cands:
        if buys >= 3 or len(held) >= 15:
            break
        if c["code"] in held or c["code"] in pend or c["score"] < 60:
            continue
        try:
            lv = signals.levels(c["code"])
        except Exception:  # noqa: BLE001
            continue
        q = lv["quote"] or {}
        px, vwap, rsi5, pct = lv["price"], lv["vwap"], lv["rsi5"], q.get("pct") or 0
        if not (px and vwap) or px <= vwap or not (45 <= (rsi5 or 0) <= 75) or not (0.5 <= pct <= 7):
            continue
        budget = min(v["equity"] * 0.06 * min(1.3, c["score"] / 70), v["cash"] * 0.98)
        qty = int(budget / (px * 1.003) / 100) * 100
        if qty < 100:
            break
        pl = c["plan"]
        try:
            sim.place("ai", c["code"], "buy", qty, stop=pl["stop"], target1=pl["target1"], target2=pl["target2"], emotion="跟随系统",
                      reason=f"高频·日内进场：评分 {c['score']:.0f}，价格 {px} 站上均价 {vwap}，5分钟RSI {rsi5:.0f}，涨幅 {pct:+.1f}%"
                             + (f"，信号：{c['signals'][0]['name']}" if c["signals"] else ""))
            v["cash"] -= qty * px
            held[c["code"]] = {}
            buys += 1
            acted.append(f"买 {c['name']}")
        except ValueError as e:
            _log(f"买入失败 {c['name']}: {e}")
    if acted:
        _log("；".join(acted))
    return {"acted": acted}


def status() -> dict:
    return {"mode": mode(), **{k: v for k, v in STATE.items() if k != "running"}}
