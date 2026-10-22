"""个股策略回测：在历史K线上逐日“假装回到当天”，用当时能看到的数据判断信号，
次日开盘买入，按同一套止损/止盈/时间止损规则离场。不偷看未来。"""
from __future__ import annotations

from . import market
from .config import load_settings
from .factors import raw_factors, score
from .sim import fees
from .strategies import NAMES, detect


def run(code: str, strategy: str = "score", days: int = 500, threshold: float = 68.0) -> dict:
    code = market.norm_code(code)
    rows = market.kline(code, days + 80)
    if len(rows) < 120:
        raise ValueError("历史数据不足 120 根K线")
    risk = load_settings()["risk"]
    trades, i, start = [], 70, 70
    equity, curve = 1.0, []
    bench0 = rows[start]["close"]
    while i < len(rows) - 1:
        hist = rows[max(0, i - 130):i + 1]
        raw, ind = raw_factors(hist, None)
        sigs = detect(hist, ind, None, "close", {})
        if strategy == "score":
            sc, _ = score(raw)
            hit = sc >= threshold and bool(sigs)
            why = f"评分 {sc:.0f}" + (f" + {sigs[0]['name']}" if sigs else "")
        else:
            m = [s for s in sigs if s["id"] == strategy]
            hit = bool(m)
            why = m[0]["why"] if m else ""
        curve.append({"date": rows[i]["date"], "eq": round(equity, 4), "bench": round(rows[i]["close"] / bench0, 4)})
        if not hit:
            i += 1
            continue
        a = ind["atr"][-1] or rows[i]["close"] * 0.03
        entry_day = i + 1
        entry = rows[entry_day]["open"]
        if entry >= rows[i]["close"] * (1 + market.limit_pct(code) - 0.002):
            i += 1  # 一字涨停买不进
            continue
        stop = max(entry - risk["stop_atr"] * a, entry * (1 - risk["max_stop_pct"]))
        R = entry - stop
        t1, t2 = entry + risk["rr_1"] * R, entry + risk["rr_2"] * R
        half, exit_px, exit_i, how = False, None, None, ""
        realized = 0.0
        for j in range(entry_day + 1, min(len(rows), entry_day + risk["max_hold_days"] + 1)):  # T+1
            r = rows[j]
            if r["low"] <= stop:
                exit_px, exit_i, how = min(stop, r["open"]), j, "止损" if not half else "保本/移动止损"
                break
            if r["high"] >= t1 and not half:
                realized += 0.5 * (t1 / entry - 1)
                half, stop = True, max(stop, entry)
            if r["high"] >= t2 and half:
                exit_px, exit_i, how = t2, j, "第二止盈"
                break
        if exit_px is None:
            exit_i = min(len(rows) - 1, entry_day + risk["max_hold_days"])
            exit_px, how = rows[exit_i]["close"], "时间止损"
        part = 0.5 if half else 1.0
        ret = realized + part * (exit_px / entry - 1)
        cost = (fees("buy", 1e5) + fees("sell", 1e5)) / 1e5
        ret -= cost
        equity *= 1 + ret
        trades.append({"buy_date": rows[entry_day]["date"], "buy": round(entry, 2), "sell_date": rows[exit_i]["date"],
                       "sell": round(exit_px, 2), "ret": round(ret * 100, 2), "exit": how, "why": why,
                       "days": exit_i - entry_day})
        for k in range(i + 1, exit_i + 1):
            curve.append({"date": rows[k]["date"], "eq": round(equity, 4), "bench": round(rows[k]["close"] / bench0, 4)})
        i = exit_i + 1
    rets = [t["ret"] for t in trades]
    wins = [x for x in rets if x > 0]
    peak, mdd = 0.0, 0.0
    for c in curve:
        peak = max(peak, c["eq"])
        mdd = max(mdd, 1 - c["eq"] / peak if peak else 0)
    n_days = len(curve) or 1
    total = equity - 1
    return {"code": code, "strategy": strategy, "strategy_name": NAMES.get(strategy, "综合评分"),
            "trades": trades, "curve": curve, "n": len(trades), "total": round(total * 100, 2),
            "annual": round(((1 + total) ** (250 / n_days) - 1) * 100, 2) if total > -1 else -100,
            "win_rate": round(len(wins) / len(rets), 3) if rets else None,
            "avg": round(sum(rets) / len(rets), 2) if rets else None, "max_dd": round(mdd * 100, 2),
            "bench": round((rows[-1]["close"] / bench0 - 1) * 100, 2), "start": rows[start]["date"],
            "end": rows[-1]["date"],
            "verdict": verdict(len(trades), total, mdd, len(wins) / len(rets) if rets else 0)}


def verdict(n, total, mdd, win) -> str:
    if n < 5:
        return "交易次数太少（<5），结果没有统计意义，换个策略或股票再看"
    if total > 0.15 and mdd < 0.2 and win >= 0.45:
        return "这个策略在这只股票上历史表现不错，可以作为参考（历史不代表未来）"
    if total > 0:
        return "小幅盈利，优势不明显；可以结合其他信号使用"
    return "历史上这个策略在这只股票上亏钱，不建议使用"
