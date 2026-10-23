"""多维度因子：原始值 -> 0~100 分（logistic 映射，个股之间可比），再按权重合成。

权重由 learn.py 根据实战样本的 IC 自动迭代；这里只负责计算。
"""
from __future__ import annotations

import math

from . import db
from .indicators import pack, slope

# (key, 中文名, center, scale, 说明)
SPECS = [
    ("momentum", "动量", 3.0, 6.0, "近5日与20日涨幅的平均；越强说明资金在持续推升"),
    ("trend", "趋势", 2.0, 1.2, "均线多头排列程度 + 20日线斜率；趋势向上的股票更容易延续"),
    ("volume", "量能", 1.3, 0.5, "量比（今日成交相对近5日的放大倍数），温和放量最好，过度放量扣分"),
    ("capital", "资金", 0.0, 5.0, "主力（大单+超大单）净流入占成交额比例，今日与近5日加权"),
    ("strength", "强度", 1.0, 2.5, "相对大盘的超额涨幅 + 当日收在高位的程度"),
    ("volatility", "波动", -1.5, 0.8, "ATR波动率接近3.5%最合适：太小没机会，太大风险高"),
    ("value", "估值", 0.6, 0.35, "市盈率越低越安全；亏损股（PE<0）明显扣分"),
    ("position", "位置", 88.0, 6.0, "距60日最高价的位置，接近新高更好；短线过热（RSI、乖离过大）扣分"),
    ("liquidity", "流动性", 8.3, 0.4, "近5日平均成交额（对数）；太冷门的股票买卖都困难"),
    ("sector", "板块", 50.0, 15.0, "所属行业的强度（今日涨幅、上涨家数、涨停数、近5日动量的排名合成）；强行业里的个股更容易走强"),
]
FORMULA = {
    "momentum": "x = 0.5·R5 + 0.5·R20（R_n = Cₜ/Cₜ₋ₙ − 1，单位%）；分数 = 100/(1+e^(−(x−3)/6))",
    "trend": "x = [MA5>MA10] + [MA10>MA20] + [MA20>MA60] + [C>MA20] + 5·斜率(MA20,10日)；分数 = σ((x−2)/1.2)·100",
    "volume": "VR = 量比（封顶6）；x = VR（VR≤3），否则 3 − 0.8·(VR−3)；分数 = σ((x−1.3)/0.5)·100",
    "capital": "x = 0.6·今日主力净占比 + 0.4·近5日主力净占比均值（%）；分数 = σ(x/5)·100",
    "strength": "x = (今日涨幅 − 市场中位涨幅) + 2·(日内位置 − 0.5)；日内位置 = (C−L)/(H−L)；分数 = σ((x−1)/2.5)·100",
    "volatility": "ATR% = ATR14/C·100；x = −|ATR% − 3.5|；分数 = σ((x+1.5)/0.8)·100",
    "value": "x = 2 − log₁₀(PE)（PE≤0 时 x = −1）；分数 = σ((x−0.6)/0.35)·100",
    "position": "x = 100·C/H60 − max(0, RSI6−80) − 2·max(0, 乖离20−12%)；分数 = σ((x−88)/6)·100",
    "liquidity": "x = log₁₀(近5日平均成交额)；分数 = σ((x−8.3)/0.4)·100（约2亿元对应50分）",
    "sector": "x = 行业强度 = 0.40·Rank(今日涨幅) + 0.20·Rank(上涨比例) + 0.15·Rank(涨停数) + 0.25·Rank(5日动量)；分数 = σ((x−50)/15)·100",
}
COMPOSITE = "综合分 = 0.75·Σ(wᵢ·因子分ᵢ)/Σwᵢ + 0.25·策略分；策略分 = 80·最强信号(置信度×权重) + 15·(第2、3强之和)；σ(z)=1/(1+e^(−z))"
KEYS = [s[0] for s in SPECS]
LABELS = {s[0]: s[1] for s in SPECS}
EXPLAIN = {s[0]: s[4] for s in SPECS}

DEFAULT_WEIGHTS = {"momentum": 0.13, "trend": 0.16, "volume": 0.10, "capital": 0.14, "strength": 0.12,
                   "volatility": 0.06, "value": 0.08, "position": 0.11, "liquidity": 0.10,
                   "sector": 0.08}


def current_weights() -> tuple[int, dict]:
    r = db.q1("SELECT version, weights FROM weights ORDER BY version DESC LIMIT 1")
    if not r:
        return 0, dict(DEFAULT_WEIGHTS)
    w = db.loads(r["weights"], DEFAULT_WEIGHTS)
    return r["version"], {k: float(w.get(k, DEFAULT_WEIGHTS[k])) for k in KEYS}


def _sig(x: float, c: float, s: float) -> float:
    z = (x - c) / s
    z = max(-30, min(30, z))
    return 100 / (1 + math.exp(-z))


def raw_factors(rows: list[dict], live: dict | None, market_median: float = 0.0,
                flows: list[dict] | None = None) -> tuple[dict, dict]:
    """rows: 日K（最后一根可以是今天盘中）；live: 实时行情行。返回 (raw, ind)。"""
    ind = pack(rows)
    c = ind["close"]
    n = len(c)
    last = c[-1]

    def ret(k):
        return (last / c[-1 - k] - 1) * 100 if n > k and c[-1 - k] else 0.0

    r5, r20 = ret(5), ret(20)
    ma5, ma10, ma20, ma60 = (ind[k][-1] for k in ("ma5", "ma10", "ma20", "ma60"))
    align = sum([bool(ma5 and ma10 and ma5 > ma10), bool(ma10 and ma20 and ma10 > ma20),
                 bool(ma20 and ma60 and ma20 > ma60), bool(ma20 and last > ma20)])
    trend = align + slope(ind["ma20"], 10) * 5

    vr = (live or {}).get("vr")
    if vr is None:
        v5 = ind["vma5"][-2] if n > 6 else None
        vr = (rows[-1]["vol"] / v5) if v5 else 1.0
    vr = min(vr, 6.0)
    volume = vr if vr <= 3 else 3 - (vr - 3) * 0.8

    mp = (live or {}).get("main_pct")
    m5 = None
    if flows:
        last5 = [f["main_pct"] for f in flows[-5:] if f.get("main_pct") is not None]
        m5 = sum(last5) / len(last5) if last5 else None
    capital = (mp or 0) * 0.6 + (m5 if m5 is not None else (mp or 0)) * 0.4

    pct = (live or {}).get("pct")
    if pct is None:
        pct = ret(1)
    hi = (live or {}).get("high") or rows[-1]["high"]
    lo = (live or {}).get("low") or rows[-1]["low"]
    pos_day = (last - lo) / (hi - lo) if hi and lo and hi > lo else 0.5
    strength = (pct - market_median) + 2 * (pos_day - 0.5)

    a = ind["atr"][-1]
    atrp = (a / last * 100) if a and last else 3.5
    volatility = -abs(atrp - 3.5)

    pe = (live or {}).get("pe")
    value = (2 - math.log10(pe)) if pe and pe > 0 else -1.0

    h60 = max(r["high"] for r in rows[-60:])
    rsi6 = ind["rsi6"][-1] or 50
    dev20 = (last / ma20 - 1) * 100 if ma20 else 0
    position = 100 * last / h60 - max(0, rsi6 - 80) * 1.0 - max(0, dev20 - 12) * 2.0

    amts = [r["vol"] * r["close"] for r in rows[-6:-1]] or [rows[-1]["vol"] * last]
    liquidity = math.log10(max(sum(amts) / len(amts), 1.0))

    raw = {"momentum": 0.5 * r5 + 0.5 * r20, "trend": trend, "volume": volume, "capital": capital,
           "strength": strength, "volatility": volatility, "value": value, "position": position,
           "liquidity": liquidity, "sector": 50.0}  # 板块强度由 analysis.evaluate 用实时行业数据覆盖
    extra = {"r5": r5, "r20": r20, "vr": vr, "atrp": atrp, "rsi6": rsi6, "dev20": dev20, "pos_day": pos_day,
             "h60": h60, "pct": pct, "main_pct": mp, "main5": m5, "align": align}
    ind["extra"] = extra
    return raw, ind


def score(raw: dict, weights: dict | None = None) -> tuple[float, dict]:
    weights = weights or current_weights()[1]
    parts = {}
    for k, _, c, s, _ in SPECS:
        parts[k] = round(_sig(raw[k], c, s), 1)
    tot = sum(weights.values()) or 1
    comp = sum(parts[k] * weights[k] for k in KEYS) / tot
    return round(comp, 1), parts
