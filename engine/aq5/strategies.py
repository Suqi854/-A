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


def s_auction_gap_hold(rows, ind, live, session):
    """竞价站上昨高：高开且开盘价高于昨日最高，量比放大。"""
    if session != "auction" or not live or len(rows) < 3:
        return None
    o, pv = live.get("open") or live.get("price"), live.get("prev")
    yh = rows[-2]["high"]
    if not (o and pv):
        return None
    gap = (o / pv - 1) * 100
    vr = ind["extra"]["vr"]
    if 0.5 <= gap <= 4 and o > yh and vr >= 2:
        return 0.55 + min(0.25, (vr - 2) * 0.04), f"竞价高开 {gap:.1f}% 且开盘价 {o:.2f} 站上昨日最高 {yh:.2f}，量比 {vr:.1f}：跳空强势"
    return None


def s_auction_flat_volume(rows, ind, live, session):
    """竞价平开但量能异常放大，且在20日线上方：资金悄悄介入。"""
    if session != "auction" or not live:
        return None
    o, pv = live.get("open") or live.get("price"), live.get("prev")
    m20 = _g(ind["ma20"])
    if not (o and pv and m20):
        return None
    gap = (o / pv - 1) * 100
    vr = ind["extra"]["vr"]
    if abs(gap) < 1 and vr >= 5 and pv >= m20:
        return 0.5 + min(0.3, (vr - 5) * 0.03), f"竞价平开（{gap:+.1f}%）但竞价量比高达 {vr:.1f}，昨收在20日线上：有资金提前介入"
    return None


def s_close_new_high(rows, ind, live, session):
    """尾盘创日内新高 + 放量 + 站上均价。"""
    if session != "close" or not live:
        return None
    e = ind["extra"]
    avg, price = live.get("avg"), live.get("price")
    if avg and price and e["pos_day"] > 0.95 and 2 <= e["pct"] <= 6 and e["vr"] >= 1.5 and price > avg:
        return 0.6, f"尾盘收在日内最高附近（日内位置 {e['pos_day']:.2f}），涨 {e['pct']:.1f}%、量比 {e['vr']:.1f}，站上均价 {avg:.2f}"
    return None


def s_close_vwap(rows, ind, live, session):
    """尾盘回踩均价线企稳：温和上涨、贴着均价线、短均线多头。"""
    if session != "close" or not live:
        return None
    e = ind["extra"]
    avg, price = live.get("avg"), live.get("price")
    m5, m10 = _g(ind["ma5"]), _g(ind["ma10"])
    if avg and price and m5 and m10 and 0 <= e["pct"] <= 3 and avg <= price <= avg * 1.005 and 0.4 <= e["pos_day"] <= 0.8 and m5 > m10:
        return 0.5, f"尾盘回到均价线 {avg:.2f} 上方企稳，涨幅温和 {e['pct']:.1f}%，MA5>MA10：低风险的尾盘买点"
    return None


SESSION_OF = {"auction_strong": "auction", "auction_gap_hold": "auction", "auction_flat_volume": "auction",
              "tail_strong": "close", "close_new_high": "close", "close_vwap": "close"}


def _m(rows, ind):
    if "models" not in ind:
        from .models import compute
        ind["models"] = compute(rows, ind)
    return ind["models"]


def s_reg_lower(rows, ind, live, session):
    """回归通道低吸：上升趋势中回落到回归通道下沿。"""
    r = _m(rows, ind).get("reg")
    e = ind["extra"]
    if r and r["slope_ann"] > 15 and r["r2"] > 0.5 and r["z"] < -1.3 and e["pct"] > -1:
        conf = min(0.9, 0.5 + (-1.3 - r["z"]) * 0.2 + (r["r2"] - 0.5) * 0.4)
        return conf, (f"60日对数回归通道向上（年化 {r['slope_ann']:+.0f}%，R² {r['r2']:.2f}），价格回落到通道下沿 "
                      f"z={r['z']:+.2f}（下轨 {r['lower']}），统计上偏离过大、易回归")
    return None


def s_mean_revert(rows, ind, live, session):
    """均值回归：Z20<-2 且 Hurst<0.5、半衰期短。"""
    m = _m(rows, ind)
    z, h, hl = m.get("z20"), m.get("hurst"), m.get("half_life")
    if z is not None and z < -2 and h is not None and h < 0.5 and hl and hl < 20 and ind["extra"]["pct"] > -2:
        return 0.5 + min(0.3, (-2 - z) * 0.15 + (0.5 - h)), (f"Z20={z:+.2f}（低于均值 {abs(z):.1f} 个标准差），Hurst={h:.2f}<0.5 "
                                                              f"属均值回归型，半衰期 {hl} 天：短线有回归均值的统计优势")
    return None


def s_squeeze_break(rows, ind, live, session):
    """波动收敛后放量突破布林上轨。"""
    b = _m(rows, ind).get("bbw")
    c, e = ind["close"], ind["extra"]
    up = ind["boll_up"]
    if not b or up[-2] is None:
        return None
    prev_sq = b.get("recent_min_pctile", 100) <= 20
    if prev_sq and c[-1] > (up[-1] or 1e9) * 0.995 and e["vr"] >= 1.3 and e["pct"] > 1:
        return 0.55 + min(0.3, (e["vr"] - 1.3) * 0.1), (f"布林带宽前几天处于近120日 {b['recent_min_pctile']:.0f}% 低分位（波动收敛），"
                                                        f"今日放量（量比 {e['vr']:.1f}）冲击上轨 {up[-1]:.2f}：方向选择向上")
    return None


def s_trend_quality(rows, ind, live, session):
    """高质量趋势：斜率大、R²高、Hurst>0.55、且未过热。"""
    m = _m(rows, ind)
    r, h = m.get("reg"), m.get("hurst")
    c = ind["close"]
    m20 = _g(ind["ma20"])
    if r and h and r["slope_ann"] > 30 and r["r2"] > 0.75 and h > 0.55 and m20 and c[-1] > m20 and r["z"] < 1.2:
        return min(0.85, 0.5 + (r["r2"] - 0.75) + (h - 0.55)), (f"趋势质量高：年化斜率 {r['slope_ann']:+.0f}%、R² {r['r2']:.2f}、"
                                                               f"Hurst {h:.2f}（趋势延续型），价格在通道 z={r['z']:+.2f} 未过热")
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
    ("auction_gap_hold", "竞价站上昨高", s_auction_gap_hold, "早盘策略：高开且开盘就站上昨日最高价，强势跳空"),
    ("auction_flat_volume", "竞价平开放量", s_auction_flat_volume, "早盘策略：平开但竞价量异常放大，资金提前埋伏"),
    ("close_new_high", "尾盘创新高", s_close_new_high, "尾盘策略：收盘前创出日内新高并放量，次日惯性较强"),
    ("close_vwap", "尾盘回踩均价", s_close_vwap, "尾盘策略：温和上涨、尾盘回踩均价线企稳，风险较小"),
    ("reg_lower", "回归通道低吸", s_reg_lower, "上升的对数回归通道里，价格跌到下沿（统计意义上的超跌），等待回归中轨"),
    ("mean_revert", "均值回归", s_mean_revert, "价格偏离20日均值超过2个标准差，且该股属于均值回归型（Hurst<0.5）"),
    ("squeeze_break", "收敛突破", s_squeeze_break, "布林带长期收窄后放量突破上轨，波动率扩张往往伴随趋势行情"),
    ("trend_quality", "趋势质量", s_trend_quality, "斜率、拟合度、Hurst 三者都显示趋势强且稳定，顺势持有"),
]
FORMULAS = {
    "breakout": "Cₜ > max(H₋₂₀..H₋₁) 且 量比 ≥ 1.5 且 MA20ₜ > MA20ₜ₋₅；置信度 = 0.5 + 0.15·(量比−1.5) + 5·(Cₜ/H20 − 1)",
    "pullback": "MA10 > MA20 且 min(|C/MA10−1|, |C/MA20−1|) < 2% 且 VMA5 < 0.85·VMA20 且 5日涨幅 < 2%",
    "volume_start": "2% ≤ 涨幅 ≤ 7% 且 1.8 ≤ 量比 ≤ 5 且 3% ≤ 换手 ≤ 18% 且 20日涨幅 < 25%",
    "oversold": "RSI6 < 25 且 (C/MA20 − 1) < −8% 且 今日收红 且 收盘位于日内区间上半部",
    "macd_cross": "DIFₜ₋₁ ≤ DEAₜ₋₁ 且 DIFₜ > DEAₜ；DIF = EMA12 − EMA26，DEA = EMA9(DIF)；|DIF|/C < 1% 视为零轴附近",
    "accumulate": "近5日主力净流入占比均值 > 3% 且 |20日涨幅| < 8% 且 ATR% < 5%",
    "tail_strong": "14:45 时 价格 > 1.005·均价线 且 3% ≤ 涨幅 ≤ 7% 且 日内位置 > 0.8 且 量比 ≥ 1.5",
    "auction_strong": "1% ≤ 竞价高开 ≤ 5% 且 竞价量比 ≥ 3 且 昨收 ≥ MA20",
    "auction_gap_hold": "0.5% ≤ 竞价高开 ≤ 4% 且 开盘价 > 昨日最高 且 竞价量比 ≥ 2",
    "auction_flat_volume": "|竞价涨幅| < 1% 且 竞价量比 ≥ 5 且 昨收 ≥ MA20",
    "close_new_high": "14:45 时 日内位置 > 0.95 且 2% ≤ 涨幅 ≤ 6% 且 量比 ≥ 1.5 且 价格 > 均价",
    "close_vwap": "14:45 时 0 ≤ 涨幅 ≤ 3% 且 均价 ≤ 价格 ≤ 1.005·均价 且 0.4 ≤ 日内位置 ≤ 0.8 且 MA5 > MA10",
    "reg_lower": "ln P = a + b·t（60日）；年化斜率 > 15% 且 R² > 0.5 且 通道 z < −1.3",
    "mean_revert": "Z20 = (C − MA20)/σ20 < −2 且 Hurst < 0.5 且 半衰期 = −ln2/β < 20 天",
    "squeeze_break": "BBW = (上轨−下轨)/中轨 在前5日内曾处于近120日 ≤20% 分位 且 C ≥ 0.995·上轨 且 量比 ≥ 1.3",
    "trend_quality": "年化斜率 > 30% 且 R² > 0.75 且 Hurst > 0.55 且 C > MA20 且 通道 z < 1.2",
}
NAMES = {s[0]: s[1] for s in STRATEGIES}
DESC = {s[0]: s[3] for s in STRATEGIES}


def strategy_weights(session: str | None = None) -> dict:
    """来自最近一次学习结果的策略权重（默认 1.0）；指定场次且该场次样本≥10时用场次专属权重。"""
    r = db.q1("SELECT strat FROM weights ORDER BY version DESC LIMIT 1")
    w = db.loads(r["strat"], {}) if r else {}
    by = ((w.get("_by_session") or {}).get(session) or {}) if session else {}
    out = {}
    for k, *_ in STRATEGIES:
        x = by.get(k) if isinstance(by.get(k), dict) and (by[k].get("n") or 0) >= 10 else w.get(k)
        out[k] = float(x.get("weight", 1.0)) if isinstance(x, dict) else 1.0
    return out


def detect(rows, ind, live, session="intraday", sw: dict | None = None) -> list[dict]:
    sw = sw or strategy_weights()
    out = []
    for key, name, fn, *_ in STRATEGIES:
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
