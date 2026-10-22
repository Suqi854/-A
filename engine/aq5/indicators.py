"""技术指标（纯 Python）。输入为按日期升序的列表。"""
from __future__ import annotations

import math


def ma(x: list[float], n: int) -> list[float | None]:
    out, s = [], 0.0
    for i, v in enumerate(x):
        s += v
        if i >= n:
            s -= x[i - n]
        out.append(s / n if i >= n - 1 else None)
    return out


def ema(x: list[float], n: int) -> list[float]:
    out, k = [], 2 / (n + 1)
    for i, v in enumerate(x):
        out.append(v if i == 0 else out[-1] + k * (v - out[-1]))
    return out


def macd(close: list[float], f=12, s=26, sig=9):
    e1, e2 = ema(close, f), ema(close, s)
    dif = [a - b for a, b in zip(e1, e2)]
    dea = ema(dif, sig)
    hist = [2 * (a - b) for a, b in zip(dif, dea)]
    return dif, dea, hist


def rsi(close: list[float], n=6) -> list[float | None]:
    out, up, dn = [None], 0.0, 0.0
    for i in range(1, len(close)):
        d = close[i] - close[i - 1]
        u, w = max(d, 0), max(-d, 0)
        if i <= n:
            up += u / n
            dn += w / n
        else:
            up = (up * (n - 1) + u) / n
            dn = (dn * (n - 1) + w) / n
        out.append(None if i < n else (100.0 if dn == 0 else 100 - 100 / (1 + up / dn)))
    return out


def atr(rows: list[dict], n=14) -> list[float | None]:
    trs, out = [], []
    for i, r in enumerate(rows):
        pc = rows[i - 1]["close"] if i else r["close"]
        trs.append(max(r["high"] - r["low"], abs(r["high"] - pc), abs(r["low"] - pc)))
        if i < n - 1:
            out.append(None)
        elif i == n - 1:
            out.append(sum(trs) / n)
        else:
            out.append((out[-1] * (n - 1) + trs[-1]) / n)
    return out


def boll(close: list[float], n=20, k=2.0):
    m = ma(close, n)
    up, dn = [], []
    for i in range(len(close)):
        if m[i] is None:
            up.append(None)
            dn.append(None)
            continue
        w = close[i - n + 1:i + 1]
        sd = math.sqrt(sum((v - m[i]) ** 2 for v in w) / n)
        up.append(m[i] + k * sd)
        dn.append(m[i] - k * sd)
    return m, up, dn


def kdj(rows: list[dict], n=9):
    k_, d_, j_ = [], [], []
    k, d = 50.0, 50.0
    for i in range(len(rows)):
        w = rows[max(0, i - n + 1):i + 1]
        hh, ll = max(r["high"] for r in w), min(r["low"] for r in w)
        rsv = 50.0 if hh == ll else (rows[i]["close"] - ll) / (hh - ll) * 100
        k = 2 / 3 * k + rsv / 3
        d = 2 / 3 * d + k / 3
        k_.append(k)
        d_.append(d)
        j_.append(3 * k - 2 * d)
    return k_, d_, j_


def slope(x: list[float | None], n: int) -> float:
    """最近 n 个点的线性回归斜率 / 均值（%/天）。"""
    w = [v for v in x[-n:] if v is not None]
    if len(w) < 3:
        return 0.0
    m = len(w)
    xm = (m - 1) / 2
    ym = sum(w) / m
    num = sum((i - xm) * (v - ym) for i, v in enumerate(w))
    den = sum((i - xm) ** 2 for i in range(m))
    return (num / den) / ym * 100 if ym else 0.0


def pack(rows: list[dict]) -> dict:
    """计算一组常用指标，供分析/选股复用。"""
    c = [r["close"] for r in rows]
    v = [r["vol"] for r in rows]
    dif, dea, hist = macd(c)
    m, bu, bd = boll(c)
    k, d, j = kdj(rows)
    return {"close": c, "vol": v, "ma5": ma(c, 5), "ma10": ma(c, 10), "ma20": ma(c, 20), "ma60": ma(c, 60),
            "vma5": ma(v, 5), "vma20": ma(v, 20), "dif": dif, "dea": dea, "hist": hist, "rsi6": rsi(c, 6),
            "rsi14": rsi(c, 14), "atr": atr(rows), "boll_up": bu, "boll_dn": bd, "k": k, "d": d, "j": j}
