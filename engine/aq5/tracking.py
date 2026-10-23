"""选股跟踪：每只入选股之后 10 个交易日的真实走势，判断是否符合预期，并归因“为什么”。

判定（以选出价为基准）：
  符合预期：先触及止盈①，或 3 日收益 ≥ +2%，或 3 日超额收益（相对沪深300）≥ +2%
  不符合：先触及止损，或 3 日收益 ≤ −2% 且跑输大盘
  一般：介于两者之间；跟踪中：还不满 3 个交易日
归因因素：大盘同期、所属行业、追高（选出当日涨幅）、次日高开低走、量能是否延续、主策略历史胜率、
          因子在赢/输样本中的差异（哪些因子高分却没用）。
"""
from __future__ import annotations

import time

from . import db, market
from .config import today
from .factors import LABELS

_cache = {"t": 0.0, "key": None, "data": None}


def _path(code: str, date: str, base: float, stop, t1, n: int = 10) -> dict:
    rows = market.kline(code, 160, history=True)
    after = [r for r in rows if r["date"] > date][:n]
    day0 = next((r for r in rows if r["date"] == date), None)
    out = {"days": len(after), "path": [round((r["close"] / base - 1) * 100, 2) for r in after], "first": None,
           "max_up": None, "max_dn": None, "r1": None, "r3": None, "r5": None, "gap1": None, "fade1": False, "vol_ratio": None}
    if not after:
        return out
    first = None
    for i, r in enumerate(after):
        if stop and r["low"] <= stop:
            first = ("stop", i + 1)
            break
        if t1 and r["high"] >= t1:
            first = ("target", i + 1)
            break
    out.update(first=first, max_up=round((max(r["high"] for r in after) / base - 1) * 100, 2),
               max_dn=round((min(r["low"] for r in after) / base - 1) * 100, 2),
               r1=out["path"][0], r3=out["path"][2] if len(after) >= 3 else None,
               r5=out["path"][4] if len(after) >= 5 else None,
               gap1=round((after[0]["open"] / base - 1) * 100, 2),
               fade1=after[0]["close"] < after[0]["open"] and after[0]["high"] > base * 1.01,
               vol_ratio=round(after[0]["vol"] / day0["vol"], 2) if day0 and day0["vol"] else None)
    return out


def _bench(date: str, k: int) -> float | None:
    rows = market.kline("sh000300", 160, history=True)
    i = next((j for j, r in enumerate(rows) if r["date"] == date), None)
    if i is None or i + k >= len(rows):
        return None
    return round((rows[i + k]["close"] / rows[i]["close"] - 1) * 100, 2)


def _sector(code: str, date: str, k: int) -> tuple[str | None, float | None]:
    m = db.q1("SELECT industry, ind_code FROM stock_sector WHERE code=?", (code,))
    if not m:
        return None, None
    rows = db.q("SELECT pct FROM sector_daily WHERE ind_code=? AND date>? ORDER BY date LIMIT ?", (m["ind_code"], date, k))
    return m["industry"], (round(sum(r["pct"] for r in rows), 2) if len(rows) >= min(k, 1) and rows else None)


def track(days: int = 20) -> dict:
    key = today()
    if _cache["key"] == key and time.time() - _cache["t"] < 600 and _cache["data"]:
        return _cache["data"]
    picks = db.q("SELECT p.*, r.market AS mkt FROM picks p LEFT JOIN pick_runs r ON r.id=p.run_id "
                 "WHERE p.date >= date(?, ?) ORDER BY p.id DESC LIMIT 400", (today(), f"-{days} day"))
    strat_hist = {}
    for r in db.q("SELECT strategies, ret_3d FROM picks WHERE ret_3d IS NOT NULL"):
        for s in (db.loads(r["strategies"], []) or [])[:1]:
            strat_hist.setdefault(s.get("id"), []).append(r["ret_3d"])
    items = []
    for p in picks:
        try:
            pa = _path(p["code"], p["date"], p["price"], p["stop"], p["target1"])
        except RuntimeError:
            continue
        sigs = db.loads(p["strategies"], []) or []
        main = sigs[0] if sigs else None
        parts = db.loads(p["factors"], {}) or {}
        k = min(3, pa["days"]) if pa["days"] else 0
        bench = _bench(p["date"], k) if k else None
        ind, sec = _sector(p["code"], p["date"], k) if k else (None, None)
        r3 = pa.get("r3")
        alpha = round(r3 - bench, 2) if r3 is not None and bench is not None else None
        pick_pct = None
        try:
            rows = market.kline(p["code"], 160, history=True)
            i = next((j for j, r in enumerate(rows) if r["date"] == p["date"]), None)
            if i:
                pick_pct = round((rows[i]["close"] / rows[i - 1]["close"] - 1) * 100, 2)
        except RuntimeError:
            pass
        first = pa.get("first")
        if pa["days"] < 3 and not first:
            verdict = "跟踪中"
        elif (first and first[0] == "target") or (r3 is not None and r3 >= 2) or (alpha is not None and alpha >= 2):
            verdict = "符合预期"
        elif (first and first[0] == "stop") or (r3 is not None and r3 <= -2 and (alpha is None or alpha < 0)):
            verdict = "不符合"
        else:
            verdict = "一般"
        why = []
        if bench is not None:
            why.append(f"大盘（沪深300）同期 {bench:+.2f}%：" + ("顺风" if bench > 0.5 else "逆风" if bench < -0.5 else "平淡"))
        if sec is not None:
            why.append(f"所属行业「{ind}」同期累计 {sec:+.2f}%" + ("，板块助推" if sec > 1 else "，板块拖累" if sec < -1 else ""))
        if alpha is not None:
            why.append(f"3日超额收益 {alpha:+.2f}%（个股 {r3:+.2f}% − 大盘 {bench:+.2f}%）")
        if pick_pct is not None and pick_pct > 5:
            why.append(f"选出当日已涨 {pick_pct:.1f}%：有追高成分，次日容易获利回吐")
        if pa.get("gap1") is not None:
            if pa.get("fade1"):
                why.append(f"次日高开 {pa['gap1']:+.2f}% 后冲高回落：买点后承接不足")
            elif pa["gap1"] < -1.5:
                why.append(f"次日低开 {pa['gap1']:+.2f}%：隔夜情绪转弱")
        if pa.get("vol_ratio") is not None:
            why.append(f"次日成交量是选出当日的 {pa['vol_ratio']:.2f} 倍" + ("：量能没有延续" if pa["vol_ratio"] < 0.7 else "：量能延续" if pa["vol_ratio"] > 1 else ""))
        if first:
            why.append(("第 %d 天先触及止盈①" if first[0] == "target" else "第 %d 天先触及止损") % first[1])
        if main and strat_hist.get(main.get("id")):
            xs = strat_hist[main["id"]]
            why.append(f"主策略「{main['name']}」历史 {len(xs)} 次，3日胜率 {sum(1 for x in xs if x > 0) / len(xs) * 100:.0f}%")
        items.append({"id": p["id"], "date": p["date"], "session": p["session"], "code": p["code"], "name": p["name"],
                      "tier": p.get("tier"), "rank": p.get("rank"), "score": p["score"], "price": p["price"],
                      "stop": p["stop"], "target1": p["target1"], "strategy": main["name"] if main else None,
                      "industry": ind, "verdict": verdict, "why": why, "alpha": alpha, "bench": bench, **pa,
                      "first": first[0] if first else None, "parts": parts})
    data = {"items": items, "summary": _summary(items)}
    _cache.update(t=time.time(), key=key, data=data)
    return data


def _grp(items, key):
    g = {}
    for it in items:
        if it["verdict"] == "跟踪中":
            continue
        g.setdefault(it.get(key) or "—", []).append(it)
    out = []
    for k, xs in g.items():
        r3 = [x["r3"] for x in xs if x.get("r3") is not None]
        al = [x["alpha"] for x in xs if x.get("alpha") is not None]
        out.append({"key": k, "n": len(xs), "hit": round(sum(1 for x in xs if x["verdict"] == "符合预期") / len(xs), 3),
                    "miss": round(sum(1 for x in xs if x["verdict"] == "不符合") / len(xs), 3),
                    "avg3": round(sum(r3) / len(r3), 2) if r3 else None, "alpha": round(sum(al) / len(al), 2) if al else None})
    return sorted(out, key=lambda x: -x["n"])


def _summary(items) -> dict:
    done = [x for x in items if x["verdict"] != "跟踪中"]
    reasons = {}
    for x in done:
        if x["verdict"] != "不符合":
            continue
        for w in x["why"]:
            tag = ("大盘逆风" if "逆风" in w else "板块拖累" if "板块拖累" in w else "追高" if "追高" in w else
                   "冲高回落" if "冲高回落" in w else "量能没延续" if "没有延续" in w else "触发止损" if "止损" in w else
                   "低开" if "低开" in w else None)
            if tag:
                reasons[tag] = reasons.get(tag, 0) + 1
    # 因子：赢的和输的在选出时的平均分差（> 0 说明这个因子在起作用）
    wins = [x for x in done if x["verdict"] == "符合预期"]
    loss = [x for x in done if x["verdict"] == "不符合"]
    fdiff = []
    if wins and loss:
        for k, lab in LABELS.items():
            a = [x["parts"].get(k) for x in wins if x["parts"].get(k) is not None]
            b = [x["parts"].get(k) for x in loss if x["parts"].get(k) is not None]
            if a and b:
                fdiff.append({"key": k, "label": lab, "win": round(sum(a) / len(a), 1), "loss": round(sum(b) / len(b), 1),
                              "diff": round(sum(a) / len(a) - sum(b) / len(b), 1)})
        fdiff.sort(key=lambda x: -abs(x["diff"]))
    return {"n": len(items), "done": len(done), "hit": round(len(wins) / len(done), 3) if done else None,
            "miss": round(len(loss) / len(done), 3) if done else None,
            "by_tier": _grp(items, "tier"), "by_strategy": _grp(items, "strategy"), "by_session": _grp(items, "session"),
            "fail_reasons": sorted(reasons.items(), key=lambda kv: -kv[1]), "factor_diff": fdiff,
            "advice": _advice(done, reasons, fdiff)}


def _advice(done, reasons, fdiff) -> list[str]:
    out = []
    if len(done) < 10:
        return [f"已完成跟踪的只有 {len(done)} 只，至少 10 只后结论才有参考价值"]
    tot = sum(reasons.values()) or 1
    if reasons.get("追高", 0) / tot > 0.25:
        out.append("失败里“追高”占比高：可以把选出当日涨幅上限从 7% 降到 5%，或等回踩再买")
    if reasons.get("大盘逆风", 0) / tot > 0.3:
        out.append("很多失败来自大盘逆风：空头市场应进一步收紧门槛或减少仓位")
    if reasons.get("冲高回落", 0) / tot > 0.2:
        out.append("次日冲高回落多：尾盘选股更适合，竞价选股可以只做 A 级")
    for f in fdiff[:2]:
        if f["diff"] < -5:
            out.append(f"「{f['label']}」因子在失败样本里反而更高（{f['loss']} vs {f['win']}）：权重可能偏大，迭代会自动下调")
        elif f["diff"] > 5:
            out.append(f"「{f['label']}」因子在成功样本里明显更高（{f['win']} vs {f['loss']}）：是有效因子")
    return out or ["暂未发现明显的系统性问题，继续积累样本"]
