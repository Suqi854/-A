"""个股分析：多因子评分 + 策略信号 + 明确交易计划（买点/止损/止盈）+ 主力行为解读 + 学习要点。"""
from __future__ import annotations

from . import learn, market
from .config import load_settings
from .factors import EXPLAIN, LABELS, current_weights, raw_factors, score
from .indicators import ma
from .strategies import DESC, detect, strategy_score, strategy_weights


def _r(x, n=2):
    return None if x is None else round(x, n)


def regime() -> dict:
    """大盘状态：沪深300 与 20/60 日线的关系。"""
    rows = market.kline_with_live("sh000300", 120)
    if len(rows) < 61:
        return {"state": "未知", "code": "unknown", "desc": "指数数据不足"}
    c = [r["close"] for r in rows]
    m20, m60 = ma(c, 20)[-1], ma(c, 60)[-1]
    m20p = ma(c, 20)[-6]
    if c[-1] > m20 > m60 and m20 >= m20p:
        st, code, desc = "多头", "bull", "沪深300在20日、60日线之上且20日线向上：可以积极一些"
    elif c[-1] < m20 < m60 and m20 <= m20p:
        st, code, desc = "空头", "bear", "沪深300在20日、60日线之下：以防守为主，少开新仓、仓位减半"
    else:
        st, code, desc = "震荡", "range", "方向不明：轻仓、只做高确定性机会"
    return {"state": st, "code": code, "desc": desc, "close": c[-1], "ma20": _r(m20), "ma60": _r(m60)}


def trade_plan(rows, ind, live, risk: dict | None = None) -> dict:
    risk = risk or load_settings()["risk"]
    price = (live or {}).get("price") or ind["close"][-1]
    atr = ind["atr"][-1] or price * 0.03
    lows10 = min(r["low"] for r in rows[-10:])
    sup = sorted({x for x in (ind["ma10"][-1], ind["ma20"][-1], lows10, ind["boll_dn"][-1]) if x and x < price},
                 reverse=True)
    res = sorted({x for x in (max(r["high"] for r in rows[-20:]), max(r["high"] for r in rows[-60:]),
                              ind["boll_up"][-1]) if x and x > price * 1.005})
    stop_atr = price - risk["stop_atr"] * atr
    stop_sup = (sup[0] * 0.985) if sup else stop_atr
    stop = max(stop_atr, min(stop_sup, price * 0.985))
    stop = max(stop, price * (1 - risk["max_stop_pct"]))
    R = max(price - stop, price * 0.01)
    t1, t2 = price + risk["rr_1"] * R, price + risk["rr_2"] * R
    buy_lo = max(sup[0], price - 0.5 * atr) if sup else price - 0.5 * atr
    return {"price": _r(price), "buy_zone": [_r(min(buy_lo, price)), _r(price)], "stop": _r(stop),
            "stop_pct": _r((stop / price - 1) * 100), "target1": _r(t1), "target1_pct": _r((t1 / price - 1) * 100),
            "target2": _r(t2), "target2_pct": _r((t2 / price - 1) * 100), "rr": _r((t1 - price) / R, 1),
            "atr": _r(atr, 3), "supports": [_r(x) for x in sup[:3]], "resistances": [_r(x) for x in res[:3]],
            "hold_days": risk["max_hold_days"],
            "rules": [f"止损：收盘跌破 {stop:.2f}（约 {(stop / price - 1) * 100:.1f}%）坚决卖出，不抱幻想",
                      f"第一止盈：到 {t1:.2f}（+{(t1 / price - 1) * 100:.1f}%）先卖一半，剩余止损上移到成本价",
                      f"第二止盈：到 {t2:.2f}（+{(t2 / price - 1) * 100:.1f}%）或跌破5日线清仓",
                      f"时间止损：持有 {risk['max_hold_days']} 个交易日仍没走出来，就离场换股"]}


def main_force(rows, ind, flows) -> dict:
    c = ind["close"]
    e = ind["extra"]
    ev = []
    if not flows or len(flows) < 5:
        return {"label": "数据不足", "tone": "neutral", "desc": "没有取到主力资金数据", "evidence": []}
    m5 = sum(f["main"] for f in flows[-5:])
    m10 = sum(f["main"] for f in flows[-10:])
    ch5 = (c[-1] / c[-6] - 1) * 100 if len(c) > 6 else 0
    ch10 = (c[-1] / c[-11] - 1) * 100 if len(c) > 11 else 0
    vshrink = (ind["vma5"][-1] or 0) < (ind["vma20"][-1] or 1) * 0.8
    vexpand = (ind["vma5"][-1] or 0) > (ind["vma20"][-1] or 1) * 1.3
    hi = c[-1] / e["h60"] if e["h60"] else 0
    m20 = ind["ma20"][-1] or c[-1]
    ev.append(f"近5日主力净{'流入' if m5 >= 0 else '流出'} {abs(m5) / 1e8:.2f} 亿，近10日 {m10 / 1e8:+.2f} 亿")
    ev.append(f"近5日涨跌 {ch5:+.1f}%，近10日 {ch10:+.1f}%；成交量{'放大' if vexpand else '萎缩' if vshrink else '平稳'}")
    pos5 = sum(1 for f in flows[-5:] if f["main"] > 0)
    ev.append(f"近5天中有 {pos5} 天主力净流入")
    if m5 > 0 and ch5 > 8 and vexpand:
        lab, tone, d = "拉升", "up", "主力资金流入同时股价快速上涨、放量：处在拉升阶段，注意不要追得太高"
    elif m10 > 0 and abs(ch10) < 5 and abs(ch5) < 5 and not vexpand:
        lab, tone, d = "吸筹", "up", "资金在流入但股价横盘：可能是主力在低调收集筹码"
    elif m10 > 0 and -10 < ch5 < -2 and vshrink and c[-1] >= m20 * 0.98:
        lab, tone, d = "洗盘", "neutral", "股价缩量回调、守住20日线，而中期资金仍是流入：像是洗盘，关注止跌信号"
    elif m5 > 0 and ch5 > 5:
        lab, tone, d = "资金推升", "up", "近5日主力净流入、股价明显上涨：资金在推动，但要留意是否放量持续"
    elif m5 < 0 and (hi > 0.92 or ch10 > 10):
        lab, tone, d = "派发", "down", "股价在高位而主力持续流出：有出货嫌疑，持有者注意兑现利润"
    elif m5 < 0 and c[-1] < m20:
        lab, tone, d = "撤离", "down", "主力流出且跌破20日线：资金在离场，回避为主"
    else:
        lab, tone, d = "不明显", "neutral", "资金与价格没有形成明确配合，主力意图不清晰"
    return {"label": lab, "tone": tone, "desc": d, "evidence": ev, "main5_yi": _r(m5 / 1e8),
            "main10_yi": _r(m10 / 1e8)}


def rating(total: float, ind) -> tuple[str, str, str]:
    up = (ind["ma20"][-1] or 0) <= ind["close"][-1]
    if total >= 78 and up:
        return "买入", "buy", "综合评分高且在20日线上方：可按计划分批买入，严格设好止损"
    if total >= 68:
        return "轻仓试错", "watch_buy", "条件不错但不够完美：最多用计划仓位的一半，跌破止损就走"
    if total >= 52:
        return "观望", "hold", "没有明显优势：持有者按止损持有，空仓者等更好的信号"
    if total >= 40:
        return "减仓", "reduce", "偏弱：持有者逢反弹减仓，不建议新买"
    return "回避", "avoid", "走势和资金都差：不碰；持有者执行止损"


def evaluate(code, rows, live, session="intraday", market_median=0.0, flows=None, weights=None, sw=None,
             risk=None) -> dict:
    raw, ind = raw_factors(rows, live, market_median, flows)
    if live and live.get("price"):
        try:
            from . import sectors
            st = sectors.strength(code)
            if st is not None:
                raw["sector"] = st
        except Exception:  # noqa: BLE001 - 板块数据失败时按中性处理
            pass
    fscore, parts = score(raw, weights)
    sigs = detect(rows, ind, live, session, sw)
    sscore = strategy_score(sigs)
    total = round(fscore * 0.75 + sscore * 0.25, 1)
    prob, prob_src = learn.predict(raw, sigs, total)
    return {"code": code, "score": total, "fscore": fscore, "sscore": sscore, "parts": parts,
            "raw": {k: round(v, 4) for k, v in raw.items()}, "signals": sigs, "prob": prob, "prob_src": prob_src,
            "plan": trade_plan(rows, ind, live, risk), "ind": ind}


def analyze(code: str) -> dict:
    code = market.norm_code(code)
    rows = market.kline_with_live(code, 250)
    if len(rows) < 30:
        raise ValueError("K线数据不足（新股或代码错误）")
    live = market.quotes([code]).get(code) or {}
    snap = {r["code"]: r for r in market.snapshot()} if not code.startswith(("hk", "us")) else {}
    srow = snap.get(code, {})
    live = {**srow, **{k: v for k, v in live.items() if v is not None}}
    if srow.get("main_pct") is not None:
        live["main_pct"] = srow["main_pct"]
    med = market.breadth(list(snap.values()))["median_pct"] if snap else 0.0
    flows = market.fundflow(code, 30)
    ver, w = current_weights()
    ev = evaluate(code, rows, live, "intraday", med, flows, w, strategy_weights())
    ind = ev.pop("ind")
    e = ind["extra"]
    lab, tone, advice = rating(ev["score"], ind)
    mf = main_force(rows, ind, flows)
    factors = [{"key": k, "label": LABELS[k], "score": ev["parts"][k], "weight": round(w[k], 3),
                "raw": ev["raw"][k], "explain": EXPLAIN[k]} for k in LABELS]
    strong = sorted(factors, key=lambda f: -f["score"])
    reasons = [f"{f['label']}得分 {f['score']:.0f}：{f['explain']}" for f in strong[:3] if f["score"] >= 60]
    risks = [f"{f['label']}得分仅 {f['score']:.0f}" for f in strong[::-1][:3] if f["score"] < 40]
    if e["rsi6"] > 80:
        risks.append(f"RSI6={e['rsi6']:.0f} 短线过热，追高容易被套")
    if e["dev20"] > 15:
        risks.append(f"股价高出20日线 {e['dev20']:.0f}%，乖离过大，回调概率上升")
    if mf["tone"] == "down":
        risks.append("主力资金" + mf["label"])
    try:
        from . import sectors
        sec = sectors.of_stock(code)
    except Exception:  # noqa: BLE001
        sec = None
    if sec:
        if sec["score"] >= 65:
            reasons.append(f"所属行业「{sec['name']}」{sec['tag']}（强度 {sec['score']:.0f}，排名 {sec['rank']}/{sec['total']}），顺势")
        elif sec["score"] < 30:
            risks.append(f"所属行业「{sec['name']}」{sec['tag']}（强度 {sec['score']:.0f}，排名 {sec['rank']}/{sec['total']}），逆势个股更难走强")
    lessons = [
        f"均线排列：MA5 {_r(ind['ma5'][-1])} / MA10 {_r(ind['ma10'][-1])} / MA20 {_r(ind['ma20'][-1])} / "
        f"MA60 {_r(ind['ma60'][-1])}。短均线在上、长均线在下叫“多头排列”，代表趋势向上。",
        f"RSI6 = {e['rsi6']:.0f}：高于80说明短线涨太急（超买），低于20说明跌太狠（超卖）。",
        f"MACD：DIF {ind['dif'][-1]:.3f}，DEA {ind['dea'][-1]:.3f}。DIF 在 DEA 上方代表动能偏强。",
        f"ATR（平均真实波幅）= {ev['plan']['atr']}，约占股价 {e['atrp']:.1f}%：止损一般放在 2 倍 ATR 以外，"
        f"避免被正常波动“洗”出去。",
        f"盈亏比 {ev['plan']['rr']}：赚的空间 / 亏的空间。长期看，盈亏比≥1.5 且胜率≥45% 的交易才值得做。",
        "板块效应：A股资金常按行业和题材轮动，同一行业的股票往往同涨同跌。选股先看行业强不强，再挑行业里的龙头或补涨股，胜率更高。",
    ]
    return {"code": code, "name": live.get("name"), "quote": {k: live.get(k) for k in (
                "price", "pct", "chg", "open", "high", "low", "prev", "vol", "amount", "turnover", "vr", "pe", "pb",
                "mcap", "fmcap", "main_net", "main_pct", "up_limit", "dn_limit", "time", "bids", "asks")},
            "score": ev["score"], "fscore": ev["fscore"], "sscore": ev["sscore"], "prob": ev["prob"],
            "prob_src": ev["prob_src"], "rating": lab, "tone": tone, "advice": advice, "factors": factors,
            "signals": ev["signals"], "strategy_desc": DESC, "plan": ev["plan"], "main_force": mf,
            "reasons": reasons, "risks": risks, "lessons": lessons, "regime": regime(), "weights_ver": ver,
            "sector": sec,
            "flows": [{"date": f["date"], "main": f["main"], "main_pct": f["main_pct"]} for f in flows[-20:]]}
