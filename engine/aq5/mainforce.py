"""主力行为识别：资金流（东财/新浪）+ 量价关系 + OBV/CMF/MFI + 筹码分布，多标签打分。

识别的行为（每个都有 0~100 的把握度和证据）：
  建仓吸筹、试盘、洗盘、拉升、资金进场、护盘、诱空、高位派发（出货）、诱多、资金离场、高位滞涨
没有资金流数据时只用量价指标判断，把握度会自动打折。
"""
from __future__ import annotations

PHASES = ["建仓吸筹", "试盘", "洗盘", "拉升", "高位派发", "资金离场"]
TONE = {"建仓吸筹": "up", "试盘": "neutral", "洗盘": "neutral", "拉升": "up", "资金进场": "up", "护盘": "up",
        "诱空": "up", "高位派发": "down", "诱多": "down", "资金离场": "down", "高位滞涨": "down"}
DESC = {
    "建仓吸筹": "股价在低位横盘，资金却悄悄流入、筹码在集中：主力可能在低调收集筹码",
    "试盘": "放量冲高后迅速回落、留下长上影：主力在试探上方抛压，通常之后会选择方向",
    "洗盘": "上升趋势中缩量回调、守住关键均线：主力在清洗不坚定的筹码，止跌后往往再起",
    "拉升": "放量连续上涨、均线多头发散：主力处于拉升阶段，别追太高，按计划止盈",
    "资金进场": "主力资金连续净流入并推动股价走强：有新资金进场",
    "护盘": "跌到支撑位附近出现长下影、资金没有大幅流出：有资金在下方承接",
    "诱空": "跌破支撑后很快收回：可能是主力故意打压制造恐慌，骗取廉价筹码",
    "高位派发": "股价在高位、放量但涨不动，资金流出或 OBV 背离：主力可能在出货",
    "诱多": "突破新高后很快跌回、量大而资金流出：可能是主力拉高诱多后出货",
    "资金离场": "主力资金持续流出且跌破20日线：资金在撤退，回避为主",
    "高位滞涨": "股价在高位放量却涨不动：上涨动能衰竭的信号",
}


def _obv(rows):
    out, v = [0.0], 0.0
    for i in range(1, len(rows)):
        d = rows[i]["close"] - rows[i - 1]["close"]
        v += rows[i]["vol"] if d > 0 else -rows[i]["vol"] if d < 0 else 0
        out.append(v)
    return out


def _cmf(rows, n=20):
    s_mfv, s_v = 0.0, 0.0
    for r in rows[-n:]:
        rng = r["high"] - r["low"]
        if rng <= 0:
            continue
        mfm = ((r["close"] - r["low"]) - (r["high"] - r["close"])) / rng
        s_mfv += mfm * r["vol"]
        s_v += r["vol"]
    return s_mfv / s_v if s_v else 0.0


def _mfi(rows, n=14):
    pos = neg = 0.0
    for i in range(len(rows) - n, len(rows)):
        if i < 1:
            continue
        tp = (rows[i]["high"] + rows[i]["low"] + rows[i]["close"]) / 3
        tp0 = (rows[i - 1]["high"] + rows[i - 1]["low"] + rows[i - 1]["close"]) / 3
        mf = tp * rows[i]["vol"]
        if tp > tp0:
            pos += mf
        elif tp < tp0:
            neg += mf
    return 100.0 if neg == 0 else 100 - 100 / (1 + pos / neg)


def _slope(x, n):
    if len(x) < n:
        return 0.0
    y = x[-n:]
    m = sum(y) / n
    t = (n - 1) / 2
    num = sum((i - t) * (v - m) for i, v in enumerate(y))
    den = sum((i - t) ** 2 for i in range(n)) or 1
    return num / den


def analyze(rows, ind, flows=None, chips=None) -> dict:
    c, v = ind["close"], ind["vol"]
    n = len(c)
    if n < 30:
        return {"label": "数据不足", "tone": "neutral", "desc": "K线太少，无法判断", "evidence": [], "tags": []}
    e = ind["extra"]
    m5_, m10_, m20_, m60_ = (ind[k][-1] for k in ("ma5", "ma10", "ma20", "ma60"))
    m20p = ind["ma20"][-6] if n > 25 else m20_
    vma5, vma20 = ind["vma5"][-1] or 1, ind["vma20"][-1] or 1
    ch = lambda k: (c[-1] / c[-1 - k] - 1) * 100 if n > k else 0.0  # noqa: E731
    ch3, ch5, ch10, ch20 = ch(3), ch(5), ch(10), ch(20)
    h60 = max(r["high"] for r in rows[-60:])
    l60 = min(r["low"] for r in rows[-60:])
    pos = (c[-1] - l60) / (h60 - l60) if h60 > l60 else 0.5     # 60日区间位置 0~1
    vexp, vshr = vma5 > vma20 * 1.3, vma5 < vma20 * 0.8
    obv = _obv(rows)
    obv_s = _slope(obv, 20) / (vma20 or 1)
    px_s = _slope(c, 20) / (c[-1] or 1) * 100
    cmf, mfi = _cmf(rows), _mfi(rows)
    big = [i for i in range(n - 10, n) if rows[i]["vol"] > 2 * (ind["vma20"][i - 1] or vma20)]
    big_up = sum(1 for i in big if rows[i]["close"] >= rows[i]["open"])
    big_dn = len(big) - big_up

    def shadow(r):
        body = abs(r["close"] - r["open"])
        rng = (r["high"] - r["low"]) or 1e-9
        return (r["high"] - max(r["close"], r["open"])) / rng, (min(r["close"], r["open"]) - r["low"]) / rng, body / rng
    up_sh = [shadow(r)[0] for r in rows[-5:]]
    lo_sh = [shadow(r)[1] for r in rows[-5:]]

    has_flow = bool(flows and len(flows) >= 5)
    if has_flow:
        f5 = sum(f["main"] for f in flows[-5:])
        f10 = sum(f["main"] for f in flows[-10:])
        f20 = sum(f["main"] for f in flows[-20:])
        pdays = sum(1 for f in flows[-5:] if f["main"] > 0)
        flow_in5, flow_out5 = f5 > 0, f5 < 0
    else:
        f5 = f10 = f20 = None
        pdays = None
        flow_in5, flow_out5 = cmf > 0.05, cmf < -0.05   # 无资金流时用 CMF 代替

    ev = []
    if has_flow:
        ev.append(f"主力净额：近5日 {f5 / 1e8:+.2f} 亿、10日 {f10 / 1e8:+.2f} 亿、20日 {f20 / 1e8:+.2f} 亿；近5天 {pdays} 天净流入")
    else:
        ev.append("没有取到逐日主力资金，下面只用量价指标判断（把握度打折）")
    ev.append(f"涨跌：3日 {ch3:+.1f}%、5日 {ch5:+.1f}%、10日 {ch10:+.1f}%、20日 {ch20:+.1f}%；60日区间位置 {pos * 100:.0f}%")
    ev.append(f"量能：5日均量/20日均量 = {vma5 / vma20:.2f}；近10日放量（>2倍均量）{len(big)} 天（阳 {big_up} / 阴 {big_dn}）")
    ev.append(f"OBV 20日斜率 {obv_s:+.2f}，价格20日斜率 {px_s:+.2f}%/天" +
              ("：量价背离（价涨量能流出）" if px_s > 0.1 and obv_s < -0.05 else "：量价背离（价跌量能流入）" if px_s < -0.1 and obv_s > 0.05 else ""))
    ev.append(f"CMF20 = {cmf:+.2f}（>0.05 资金偏流入，<−0.05 偏流出），MFI14 = {mfi:.0f}（>80 超买，<20 超卖）")
    if chips:
        ev.append(f"筹码：获利盘 {chips['profit_ratio']}%、平均成本 {chips['avg_cost']}、90%筹码集中度 {chips['c90']}%"
                  + ("（高度集中）" if (chips.get('c90') or 99) < 12 else "（较分散）" if (chips.get('c90') or 0) > 25 else ""))

    tags = []

    def add(lab, sc, why):
        if not has_flow:
            sc *= 0.85
        if sc >= 40:
            tags.append({"label": lab, "score": round(min(100, sc)), "tone": TONE[lab], "why": why})

    # 建仓吸筹
    sc = 0
    if pos < 0.5:
        sc += 25
    if abs(ch20) < 8:
        sc += 20
    if (has_flow and f20 > 0) or obv_s > 0.05 or cmf > 0.05:
        sc += 30
    if not vexp:
        sc += 10
    if chips and (chips.get("c90") or 99) < 15:
        sc += 15
    add("建仓吸筹", sc, "低位横盘 + 资金/OBV 流入" + (" + 筹码集中" if chips and (chips.get('c90') or 99) < 15 else ""))
    # 试盘
    spike = [i for i in big if shadow(rows[i])[0] > 0.45]
    if spike and pos < 0.75:
        add("试盘", 55 + 10 * len(spike) - (10 if ch5 > 8 else 0), f"近10日有 {len(spike)} 天放量冲高回落（长上影）")
    # 洗盘
    if m20_ and m60_ and m20_ > m60_ and m20_ >= m20p and -10 < ch5 < -1.5 and vshr and c[-1] >= m20_ * 0.97:
        add("洗盘", 60 + (15 if (has_flow and f10 >= 0) or cmf >= 0 else 0) + (10 if c[-1] >= (m10_ or 0) else 0),
            f"上升趋势中缩量回调 {ch5:.1f}%，守在20日线 {m20_:.2f} 附近")
    # 拉升
    if (ch5 > 8 or ch10 > 15) and vexp and m5_ and m10_ and m20_ and c[-1] > m5_ > m10_ > m20_:
        add("拉升", 60 + min(25, ch10) + (10 if flow_in5 else -10), f"均线多头发散、放量上涨（10日 {ch10:+.1f}%）")
    # 资金进场
    if flow_in5 and ch5 > 0 and m20_ and c[-1] > m20_ and (not has_flow or pdays >= 3):
        add("资金进场", 55 + (15 if has_flow and f5 > abs(f10 - f5) else 0) + (10 if big_up > big_dn else 0),
            "主力连续净流入并站上20日线" if has_flow else "CMF 资金流入且站上20日线")
    # 护盘
    sup = min(x for x in (m60_ or 1e9, l60 * 1.03))
    if c[-1] <= sup * 1.04 and max(lo_sh) > 0.45 and not (has_flow and f5 < 0 and abs(f5) > abs(f20)):
        add("护盘", 55 + (10 if mfi < 30 else 0), "支撑位附近出现长下影，有资金承接")
    # 诱空
    l20p = min(r["low"] for r in rows[-23:-3]) if n > 25 else l60
    broke = any(rows[i]["close"] < l20p for i in range(n - 3, n - 1))
    if broke and c[-1] > l20p and (flow_in5 or cmf > 0):
        add("诱空", 65, f"前两天跌破20日低点 {l20p:.2f} 后迅速收回")
    # 高位派发
    if pos > 0.8 and (ch20 > 15 or c[-1] / (m60_ or c[-1]) > 1.2):
        sc = 40 + (20 if flow_out5 else 0) + (15 if px_s > 0 and obv_s < 0 else 0) + (15 if big_dn >= 2 else 0) + (10 if max(up_sh) > 0.5 else 0)
        add("高位派发", sc, "高位 + 资金流出/OBV 背离/放量阴线")
    # 高位滞涨
    if pos > 0.85 and vexp and abs(ch5) < 3:
        add("高位滞涨", 60, f"高位放量（量能 {vma5 / vma20:.1f} 倍）但5日只涨 {ch5:+.1f}%")
    # 诱多
    h20p = max(r["high"] for r in rows[-23:-3]) if n > 25 else h60
    brk = any(rows[i]["close"] > h20p for i in range(n - 3, n - 1))
    if brk and c[-1] < h20p and (flow_out5 or big_dn):
        add("诱多", 65, f"前两天突破20日高点 {h20p:.2f} 后又跌回")
    # 资金离场
    if flow_out5 and m20_ and c[-1] < m20_ and (not has_flow or f10 < 0):
        add("资金离场", 60 + (15 if m20_ < m20p else 0) + (10 if big_dn > big_up else 0), "资金流出且跌破20日线")

    tags.sort(key=lambda t: -t["score"])
    if tags:
        top = tags[0]
        lab, tone, desc = top["label"], top["tone"], DESC[top["label"]]
    else:
        lab, tone, desc = "不明显", "neutral", "资金与价格没有形成明确配合，主力意图不清晰"
    phase = next((t["label"] for t in tags if t["label"] in PHASES), None)
    out = {"label": lab, "tone": tone, "desc": desc, "evidence": ev, "tags": tags[:5], "phase": phase,
           "phases": PHASES, "has_flow": has_flow,
           "indicators": {"obv_slope": round(obv_s, 3), "cmf": round(cmf, 3), "mfi": round(mfi, 1),
                          "pos60": round(pos * 100, 1), "vol_ratio": round(vma5 / vma20, 2)},
           "main5_yi": round(f5 / 1e8, 2) if f5 is not None else None,
           "main10_yi": round(f10 / 1e8, 2) if f10 is not None else None,
           "main20_yi": round(f20 / 1e8, 2) if f20 is not None else None}
    return out
