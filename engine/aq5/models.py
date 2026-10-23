"""数学模型：把“看图”变成可计算、可检验的数字。每个模型都附带公式，界面上会原样展示，方便学习。

- 对数价格线性回归通道：ln P_t = a + b·t + ε_t
- 均值回归 Z 分数与半衰期（AR(1) / OU 过程）
- Hurst 指数（R/S 重标极差法）：判断趋势延续还是均值回归
- 波动率：已实现波动率、EWMA（RiskMetrics λ=0.94）、VaR / CVaR
- 风险收益：夏普比率、最大回撤、期望值 EV、凯利仓位
- 筹码分布（CYQ）：按换手率衰减的成本分布，估算获利盘和平均成本
"""
from __future__ import annotations

import math

FORMULAS = {
    "reg": "ln Pₜ = a + b·t + εₜ；年化斜率 = e^(250·b) − 1；R² = 1 − SSE/SST；通道位置 z = εₜ / σ(ε)",
    "zscore": "Z = (Pₜ − MA20) / σ20（σ20 = 近20日收盘价标准差）",
    "half_life": "ΔPₜ = α + β·Pₜ₋₁ + uₜ（OU 过程离散化）；半衰期 = −ln2 / β（β<0 才有均值回归）",
    "hurst": "R/S 法：对多个窗口长度 n 计算 E[R(n)/S(n)] ∝ n^H；H>0.55 趋势延续，H<0.45 均值回归，≈0.5 随机游走",
    "vol": "日收益 rₜ = ln(Pₜ/Pₜ₋₁)；σ年化 = σ日·√250；EWMA：σ²ₜ = λσ²ₜ₋₁ + (1−λ)r²ₜ₋₁，λ=0.94",
    "var": "VaR95(1日) = 1.645·σ日·P（正态近似）；CVaR95 ≈ 2.063·σ日·P",
    "sharpe": "Sharpe = (平均日收益·250 − 无风险利率) / (σ日·√250)，无风险利率取 2%",
    "mdd": "最大回撤 = max over t ( 1 − Pₜ / max_{s≤t} Pₛ )",
    "ev": "EV = p·R − (1−p)·1（以 1 份止损为单位；p=上涨概率，R=盈亏比）",
    "kelly": "f* = p − (1−p)/R；实际用 半凯利 f*/2，并限制在 0~25%",
    "chips": "筹码ₜ = 筹码ₜ₋₁·(1 − 换手ₜ) + 当日成交在[最低,最高]上的三角分布·换手ₜ；获利盘 = 成本 < 现价 的筹码占比",
    "bbw": "布林带宽 BBW = (上轨 − 下轨) / 中轨；BBW 处于近120日最低15% 视为“收敛”",
}


def _mean(x):
    return sum(x) / len(x) if x else 0.0


def _std(x):
    if len(x) < 2:
        return 0.0
    m = _mean(x)
    return math.sqrt(sum((v - m) ** 2 for v in x) / (len(x) - 1))


def linreg(y: list[float]) -> tuple[float, float, float, list[float]]:
    """普通最小二乘 y = a + b·t，返回 (a, b, R², 残差)。"""
    n = len(y)
    t = list(range(n))
    mt, my = _mean(t), _mean(y)
    sxx = sum((v - mt) ** 2 for v in t) or 1e-12
    b = sum((ti - mt) * (yi - my) for ti, yi in zip(t, y)) / sxx
    a = my - b * mt
    res = [yi - (a + b * ti) for ti, yi in zip(t, y)]
    sst = sum((yi - my) ** 2 for yi in y) or 1e-12
    r2 = 1 - sum(r * r for r in res) / sst
    return a, b, r2, res


def reg_channel(close: list[float], n: int = 60) -> dict | None:
    if len(close) < n:
        return None
    y = [math.log(p) for p in close[-n:]]
    a, b, r2, res = linreg(y)
    sd = _std(res) or 1e-9
    fit_last = a + b * (n - 1)
    z = res[-1] / sd
    return {"n": n, "slope_ann": round((math.exp(b * 250) - 1) * 100, 1), "slope_day": round(b * 100, 3),
            "r2": round(r2, 3), "z": round(z, 2), "mid": round(math.exp(fit_last), 2),
            "upper": round(math.exp(fit_last + 2 * sd), 2), "lower": round(math.exp(fit_last - 2 * sd), 2),
            "line": [round(math.exp(a + b * i), 3) for i in range(n)], "sd": round(sd, 5)}


def zscore(close: list[float], n: int = 20) -> float | None:
    if len(close) < n:
        return None
    w = close[-n:]
    sd = _std(w)
    return round((close[-1] - _mean(w)) / sd, 2) if sd else 0.0


def half_life(close: list[float], n: int = 60) -> float | None:
    if len(close) < n + 1:
        return None
    p = close[-n - 1:]
    x, y = p[:-1], [p[i + 1] - p[i] for i in range(n)]
    _, b, _, _ = linreg_xy(x, y)
    if b >= 0:
        return None
    return round(-math.log(2) / b, 1)


def linreg_xy(x, y):
    mx, my = _mean(x), _mean(y)
    sxx = sum((v - mx) ** 2 for v in x) or 1e-12
    b = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y)) / sxx
    a = my - b * mx
    return a, b, None, None


def hurst(close: list[float], n: int = 120) -> float | None:
    """R/S 法估计 Hurst 指数。"""
    if len(close) < n + 1:
        return None
    r = [math.log(close[i] / close[i - 1]) for i in range(len(close) - n, len(close)) if close[i - 1] > 0]
    pts = []
    for size in (8, 12, 16, 24, 32, 48, 60):
        if size > len(r) // 2:
            break
        rs = []
        for st in range(0, len(r) - size + 1, size):
            seg = r[st:st + size]
            m = _mean(seg)
            cum, mn, mxv = 0.0, 0.0, 0.0
            for v in seg:
                cum += v - m
                mn, mxv = min(mn, cum), max(mxv, cum)
            s = _std(seg)
            if s > 0:
                rs.append((mxv - mn) / s)
        if rs:
            pts.append((math.log(size), math.log(_mean(rs))))
    if len(pts) < 3:
        return None
    _, h, _, _ = linreg_xy([p[0] for p in pts], [p[1] for p in pts])
    return round(max(0.0, min(1.0, h)), 3)


def vol_stats(close: list[float]) -> dict:
    r = [math.log(close[i] / close[i - 1]) for i in range(1, len(close)) if close[i - 1] > 0]
    if len(r) < 21:
        return {}
    sd20 = _std(r[-20:])
    var = _std(r[:20]) ** 2
    for x in r[20:]:
        var = 0.94 * var + 0.06 * x * x
    ew = math.sqrt(var)
    p = close[-1]
    w = r[-60:]
    ann_ret = _mean(w) * 250
    sd60 = _std(w) or 1e-9
    peak, mdd = close[-61] if len(close) > 61 else close[0], 0.0
    for v in close[-60:]:
        peak = max(peak, v)
        mdd = max(mdd, 1 - v / peak)
    return {"vol20_ann": round(sd20 * math.sqrt(250) * 100, 1), "ewma_ann": round(ew * math.sqrt(250) * 100, 1),
            "var95": round(1.645 * ew * p, 2), "var95_pct": round(1.645 * ew * 100, 2),
            "cvar95_pct": round(2.063 * ew * 100, 2),
            "sharpe60": round((ann_ret - 0.02) / (sd60 * math.sqrt(250)), 2), "mdd60": round(mdd * 100, 1),
            "ret60": round((math.exp(sum(w)) - 1) * 100, 1)}


def bbw_squeeze(ind: dict, n: int = 120) -> dict | None:
    up, dn, c = ind.get("boll_up") or [], ind.get("boll_dn") or [], ind["close"]
    vals = []
    for i in range(max(0, len(c) - n), len(c)):
        if up[i] is None or dn[i] is None:
            continue
        mid = (up[i] + dn[i]) / 2
        if mid:
            vals.append((up[i] - dn[i]) / mid)
    if len(vals) < 30:
        return None
    cur = vals[-1]
    rank = lambda x: sum(1 for v in vals if v <= x) / len(vals)  # noqa: E731
    pct = rank(cur)
    recent = min(rank(v) for v in vals[-6:-1])  # 最近5天里最收敛的时候
    return {"bbw": round(cur * 100, 2), "pctile": round(pct * 100, 1), "recent_min_pctile": round(recent * 100, 1),
            "squeeze": pct <= 0.15}


def kelly(p: float, rr: float) -> dict:
    rr = max(rr or 0, 0.01)
    f = p - (1 - p) / rr
    return {"full": round(f * 100, 1), "half": round(max(0.0, min(0.25, f / 2)) * 100, 1),
            "ev": round(p * rr - (1 - p), 3)}


def chips(rows: list[dict], float_shares: float | None = None, n: int = 120, bins: int = 50) -> dict | None:
    """筹码分布（CYQ）近似：按换手率衰减旧筹码，新筹码在当日[最低,最高]上做三角分布。"""
    rs = rows[-n:]
    if len(rs) < 30:
        return None
    lo = min(r["low"] for r in rs)
    hi = max(r["high"] for r in rs)
    if hi <= lo:
        return None
    step = (hi - lo) / bins
    dist = [0.0] * bins
    avgv = _mean([r["vol"] for r in rs]) or 1
    for r in rs:
        tr = (r["vol"] / float_shares) if float_shares else min(0.25, r["vol"] / avgv * 0.03)
        tr = max(0.0, min(1.0, tr))
        dist = [d * (1 - tr) for d in dist]
        a, b = r["low"], r["high"]
        mid = (r["open"] + r["close"] + r["high"] + r["low"]) / 4
        i0, i1 = int((a - lo) / step), min(bins - 1, int((b - lo) / step))
        w = []
        for i in range(i0, i1 + 1):
            px = lo + (i + 0.5) * step
            span = max(b - a, step)
            w.append(max(0.05, 1 - abs(px - mid) / span))
        sw = sum(w) or 1
        for k, i in enumerate(range(i0, i1 + 1)):
            dist[i] += tr * w[k] / sw
    tot = sum(dist) or 1
    dist = [d / tot for d in dist]
    price = rs[-1]["close"]
    prof = sum(d for i, d in enumerate(dist) if lo + (i + 0.5) * step <= price)
    avg = sum((lo + (i + 0.5) * step) * d for i, d in enumerate(dist))
    cum, q = 0.0, {}
    for i, d in enumerate(dist):
        cum += d
        for k in (0.05, 0.15, 0.5, 0.85, 0.95):
            if k not in q and cum >= k:
                q[k] = lo + (i + 0.5) * step
    c90 = (q[0.95] - q[0.05]) / (q[0.95] + q[0.05]) * 100 if q.get(0.95) else None
    c70 = (q[0.85] - q[0.15]) / (q[0.85] + q[0.15]) * 100 if q.get(0.85) else None
    peak_i = max(range(bins), key=lambda i: dist[i])
    return {"lo": round(lo, 3), "step": round(step, 4), "dist": [round(d, 5) for d in dist],
            "profit_ratio": round(prof * 100, 1), "avg_cost": round(avg, 2), "peak": round(lo + (peak_i + 0.5) * step, 2),
            "c90": round(c90, 1) if c90 is not None else None, "c70": round(c70, 1) if c70 is not None else None,
            "range90": [round(q.get(0.05, lo), 2), round(q.get(0.95, hi), 2)], "estimated": not float_shares}


def compute(rows: list[dict], ind: dict, float_shares: float | None = None) -> dict:
    """一次算齐（选股时每只股票都会调用，注意开销）。"""
    c = ind["close"]
    out = {"reg": reg_channel(c, 60), "z20": zscore(c, 20), "half_life": half_life(c, 60), "hurst": hurst(c, 120),
           "vol": vol_stats(c), "bbw": bbw_squeeze(ind)}
    h = out["hurst"]
    out["regime"] = None if h is None else ("趋势型" if h > 0.55 else "均值回归型" if h < 0.45 else "随机游走")
    return out


def summary_lines(m: dict) -> list[str]:
    """给人看的结论。"""
    out = []
    r = m.get("reg")
    if r:
        out.append(f"60日对数回归：年化斜率 {r['slope_ann']:+.1f}%，R²={r['r2']:.2f}"
                   f"（{'趋势很清晰' if r['r2'] > 0.7 else '趋势一般' if r['r2'] > 0.4 else '没有清晰趋势'}），"
                   f"当前在通道 z={r['z']:+.2f}（{'靠近上轨' if r['z'] > 1.3 else '靠近下轨' if r['z'] < -1.3 else '通道中部'}）")
    if m.get("z20") is not None:
        out.append(f"Z20={m['z20']:+.2f}：股价偏离20日均值 {abs(m['z20']):.1f} 个标准差"
                   + ("，统计上偏贵" if m["z20"] > 2 else "，统计上偏便宜" if m["z20"] < -2 else ""))
    if m.get("hurst") is not None:
        out.append(f"Hurst={m['hurst']:.2f}：{m['regime']}"
                   + ("，顺势策略更有效" if m["hurst"] > 0.55 else "，高抛低吸更有效" if m["hurst"] < 0.45 else "，走势接近随机"))
    if m.get("half_life"):
        out.append(f"均值回归半衰期约 {m['half_life']} 天：偏离均值后，一半的偏离大约这么多天会被修复")
    v = m.get("vol") or {}
    if v:
        out.append(f"波动率：20日年化 {v['vol20_ann']}%、EWMA {v['ewma_ann']}%；一天 95% 风险价值 VaR≈{v['var95_pct']}%")
        out.append(f"近60日收益 {v['ret60']:+.1f}%、夏普 {v['sharpe60']}、最大回撤 {v['mdd60']}%")
    b = m.get("bbw")
    if b:
        out.append(f"布林带宽 {b['bbw']}%，处于近120日 {b['pctile']:.0f}% 分位" + ("：波动收敛，可能酝酿方向选择" if b["squeeze"] else ""))
    return out
