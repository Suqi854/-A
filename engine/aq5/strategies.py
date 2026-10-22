"""策略库：每个策略都是一条可解释的买点规则。返回置信度 0~1 和触发理由。

策略的实战胜率会被 learn.py 统计，并据此调整各策略在综合评分中的权重。
"""
from __future__ import annotations

from . import db


def _g(x, i=-1, d=None):
    try:
        v = x[i]
        return d if v is None else v
    except (IndexError, TypeError):
        return d


def s_breakout(rows, ind, live, session):
    c, e = ind["close"], ind["extra"]
    if len(c) < 25:
        return None
    h20 = max(r["high"] for r in rows[-21:-1])
    if c[-1] > h20 and e["vr"] >= 1.5 and (ind["ma20"][-1] or 0) > (_g(ind["ma20"], -6, 0)):
        conf = min(1.0, 0.5 + (e["vr"] - 1.5) * 0.15 + (c[-1] / h20 - 1) * 5)
        return conf, f"收盘价突破20日最高 {h20:.2f}，量比 {e['vr']:.1f} 放量确认，20日线向上"
    return None


def s_pullback(rows, ind, live, session):
    c, e = ind["close"], ind["extra"]
    m5, m10, m20 = _g(ind["ma5"]), _g(ind["ma10"]), _g(ind["ma20"])
    if not (m5 and m10 and m20) or not (m10 > m20):
        return None
    near = min(abs(c[-1] / m10 - 1), abs(c[-1] / m20 - 1))
    shrink = (_g(ind["vma5"], -1, 0) or 0) < (_g(ind["vma20"], -1, 1) or 1) * 0.85
    if near < 0.02 and c[-1] >= m20 * 0.99 and e["r5"] < 2 and shrink:
        return 0.55 + (0.02 - near) * 10, (f"均线多头（MA10 {m10:.2f} > MA20 {m20:.2f}），缩量回踩到均线附近，"
                                          f"是趋势中的低吸位置")
    return None


def s_volume_start(rows, ind, live, session):
    e = ind["extra"]
    pct, tov = e["pct"], (live or {}).get("turnover") or 0
    if 2 <= pct <= 7 and 1.8 <= e["vr"] <= 5 and 3 <= tov <= 18 and e["r20"] < 25:
        return min(1.0, 0.5 + (e["vr"] - 1.8) * 0.1), (f"今日涨 {pct:.1f}%、量比 {e['vr']:.1f}、换手 {tov:.1f}%，"
                                                       f"低位放量启动（近20日涨幅仅 {e['r20']:.0f}%）")
    return None


def s_oversold(rows, ind, live, session):
    e = ind["extra"]
    r6 = e["rsi6"]
    if r6 < 25 and e["dev20"] < -8 and e["pct"] > 0 and e["pos_day"] > 0.5:
        return 0.5 + (25 - r6) * 0.01, (f"RSI6={r6:.0f} 超卖，偏离20日线 {e['dev20']:.1f}%，"
                                         f"今天收红且收在日内偏高位置，可能出现超跌反弹（短线，快进快出）")
    return None


def s_macd_cross(rows, ind, live, session):
    dif, dea = ind["dif"], ind["dea"]
    if len(dif) < 3:
        return None
    if dif[-2] <= dea[-2] and dif[-1] > dea[-1]:
        c = ind["close"][-1]
        near0 = abs(dif[-1]) / c * 100 < 1.0
        conf = 0.6 if near0 and dif[-1] > -0.002 * c else 0.45
        return conf, f"MACD 金叉（DIF {dif[-1]:.3f} 上穿 DEA {dea[-1]:.3f}）" + ("，位置在零轴附近更可靠" if near0 else "")
    return None


def s_accumulate(rows, ind, live, session):
    e = ind["extra"]
    m5 = e.get("main5")
    if m5 is not None and m5 > 3 and abs(e["r20"]) < 8 and e["atrp"] < 5:
        return 0.5 + min(0.3, (m5 - 3) * 0.05), (f"近5日主力平均净流入占比 {m5:.1f}%，但股价20日只变动 {e['r20']:.1f}%，"
                                                  f"疑似主力悄悄吸筹")
    return None


def s_tail_strong(rows, ind, live, session):
    if session != "close" or not live:
        return None
    e = ind["extra"]
    avg = live.get("avg")
    price = live.get("price")
    if avg and price and price > avg * 1.005 and 3 <= e["pct"] <= 7 and e["pos_day"] > 0.8 and e["vr"] >= 1.5:
        return 0.6, f"尾盘强势：涨 {e['pct']:.1f}%、价格站在均价线 {avg:.2f} 之上、收在当日高位，量比 {e['vr']:.1f}"
    return None


def s_auction_strong(rows, ind, live, session):
    if session != "auction" or not live:
        return None
    o, pv = live.get("open") or live.get("price"), live.get("prev")
    if not (o and pv):
        return None
    gap = (o / pv - 1) * 100
    vr = ind["extra"]["vr"]
    m20 = _g(ind["ma20"])
    if 1 <= gap <= 5 and vr >= 3 and m20 and pv >= m20:
        return 0.55 + min(0.3, (vr - 3) * 0.03), (f"竞价高开 {gap:.1f}%，竞价量比 {vr:.1f} 明显放大，且昨收在20日线之上，"
                                                  f"资金抢筹意愿强")
    return None


STRATEGIES = [
    ("breakout", "趋势突破", s_breakout, "价格突破近20日最高点并放量，说明上方抛压被消化，趋势可能加速"),
    ("pullback", "多头回踩", s_pullback, "上升趋势中缩量回落到均线，是风险较小的低吸点"),
    ("volume_start", "放量启动", s_volume_start, "低位首次明显放量上涨，常是新一波行情的开始"),
    ("oversold", "超跌反弹", s_oversold, "短期跌太多、跌太快后的技术性反弹，只做短线"),
    ("macd_cross", "MACD金叉", s_macd_cross, "快线上穿慢线，动能由弱转强；零轴附近的金叉更可靠"),
    ("accumulate", "主力吸筹", s_accumulate, "主力资金持续净流入但股价没怎么涨，可能在建仓"),
    ("tail_strong", "尾盘强势", s_tail_strong, "14:45 仍站稳均价线且收在高位，次日惯性上冲概率较高"),
    ("auction_strong", "竞价抢筹", s_auction_strong, "集合竞价高开且竞价量大，说明有资金提前抢筹"),
]
NAMES = {s[0]: s[1] for s in STRATEGIES}
DESC = {s[0]: s[3] for s in STRATEGIES}


def strategy_weights() -> dict:
    """来自最近一次学习结果的策略权重（默认 1.0）。"""
    r = db.q1("SELECT strat FROM weights ORDER BY version DESC LIMIT 1")
    w = db.loads(r["strat"], {}) if r else {}
    return {k: float((w.get(k) or {}).get("weight", 1.0)) if isinstance(w.get(k), dict) else 1.0
            for k, *_ in STRATEGIES}


def detect(rows, ind, live, session="intraday", sw: dict | None = None) -> list[dict]:
    sw = sw or strategy_weights()
    out = []
    for key, name, fn, _ in STRATEGIES:
        try:
            r = fn(rows, ind, live, session)
        except (ZeroDivisionError, TypeError, IndexError, KeyError):
            r = None
        if r:
            conf, why = r
            conf = max(0.0, min(1.0, conf))
            out.append({"id": key, "name": name, "conf": round(conf, 2), "weight": round(sw.get(key, 1.0), 2),
                        "why": why})
    out.sort(key=lambda x: -x["conf"] * x["weight"])
    return out


def strategy_score(sigs: list[dict]) -> float:
    """0~100：最强信号为主，多信号共振加分。"""
    if not sigs:
        return 0.0
    vals = sorted((s["conf"] * min(1.5, s["weight"]) for s in sigs), reverse=True)
    sc = vals[0] * 80 + sum(vals[1:3]) * 15
    return round(min(100.0, sc), 1)
