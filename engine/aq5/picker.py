"""自动选股：全主板快照 -> 粗筛 -> 逐只多因子+策略精评 -> 严格门槛（宁缺毋滥）-> 交易计划。

session: auction（9:26 集合竞价后）/ close（14:45 收盘前）/ manual（手动）
每次运行把前 150 只候选全部存为学习样本，选中的股票另存为 picks，之后自动回填涨跌，用于自我迭代。
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import analysis, db, market, notify
from .config import load_settings, now, today
from .factors import LABELS, current_weights
from .strategies import strategy_weights

STATE = {"running": False, "stage": "", "progress": 0, "total": 0, "last": None, "error": None}
_lock = threading.Lock()


def _prescore(r: dict, session: str) -> float:
    pct = r.get("pct") or 0
    vr = min(r.get("vr") or 1, 5)
    tov = r.get("turnover") or 0
    mp = r.get("main_pct") or 0
    pos = 0.5
    if r.get("high") and r.get("low") and r["high"] > r["low"]:
        pos = (r["price"] - r["low"]) / (r["high"] - r["low"])
    s = 0.0
    s += -abs(pct - 3.5) * 0.6 + (2 if 0 < pct < 8 else 0)
    s += (vr - 1) * 1.2
    s += 1 if 2 <= tov <= 15 else 0
    s += mp * 0.12
    s += pos * 1.5 if session != "auction" else 0
    return s


def _base_filter(r: dict, session: str, cfg: dict) -> bool:
    name = r.get("name") or ""
    if cfg["exclude_st"] and ("ST" in name.upper() or "退" in name):
        return False
    p = r.get("price")
    if not p or p < 2 or r.get("suspended") or not r.get("prev"):
        return False
    if r.get("listed"):
        try:
            days = (now().date() - __import__("datetime").date.fromisoformat(r["listed"])).days
            if days < cfg["min_listed_days"] * 1.45:
                return False
        except ValueError:
            pass
    pct = r.get("pct") or 0
    lim = market.limit_pct(r["code"], name) * 100
    if pct >= lim - 0.3:  # 已涨停买不进
        return False
    if pct < -4:
        return False
    if session == "auction":
        return pct <= 6
    return (r.get("amount") or 0) >= cfg["min_amount_yi"] * 1e8 * (0.75 if session == "close" else 0.5)


def run(session: str = "manual", top_n: int = 150) -> dict:
    with _lock:
        if STATE["running"]:
            return {"error": "选股正在进行中"}
        STATE.update(running=True, stage="获取全市场快照", progress=0, total=0, error=None)
    try:
        return _run(session, top_n)
    except Exception as e:  # noqa: BLE001
        STATE["error"] = str(e)
        db.event("pick", "选股失败", str(e), "error")
        raise
    finally:
        STATE["running"] = False


def _run(session: str, top_n: int) -> dict:
    s = load_settings()
    cfg = s["pick"]
    t0 = time.time()
    snap = market.snapshot(force=True)
    br = market.breadth(snap)
    reg = analysis.regime()
    pool = [r for r in snap if _base_filter(r, session, cfg)]
    pool.sort(key=lambda r: -_prescore(r, session))
    cand = pool[:top_n]
    wl = {market.norm_code(c) for c in s["watchlist"]}
    have = {r["code"] for r in cand}
    # 自选股只服务“我的模拟盘”和个人分析，自动选股 / AI 自动盘不参考自选
    STATE.update(stage=f"精评 {len(cand)} 只候选", total=len(cand))
    ver, w = current_weights()
    sw = strategy_weights(session)

    def load(r):
        try:
            return r, market.kline(r["code"], 130, history=True)
        except RuntimeError:  # 含 Blocked
            return r, []
    evaluated = []
    with ThreadPoolExecutor(max_workers=3) as ex:
        for r, rows in ex.map(load, cand):
            STATE["progress"] += 1
            if len(rows) < 60:
                continue
            if rows[-1]["date"] == today():
                rows = rows[:-1]
            live_bar = {"date": today(), "open": r.get("open") or r["price"], "close": r["price"],
                        "high": r.get("high") or r["price"], "low": r.get("low") or r["price"], "vol": r.get("vol") or 0}
            rows = rows + [live_bar]
            try:
                ev = analysis.evaluate(r["code"], rows, r, session, br["median_pct"], None, w, sw, s["risk"])
            except (ZeroDivisionError, ValueError, TypeError):
                continue
            ev["row"] = r
            evaluated.append(ev)
    evaluated.sort(key=lambda e: -e["score"])
    # 前30只补主力资金历史后再评一次（更准）
    STATE["stage"] = "补充主力资金数据"
    top = evaluated[:30]

    def refine(ev):
        try:
            flows = market.fundflow(ev["code"], 10)
            rows = market.kline(ev["code"], 130, history=True)
            if rows and rows[-1]["date"] == today():
                rows = rows[:-1]
            r = ev["row"]
            rows = rows + [{"date": today(), "open": r.get("open") or r["price"], "close": r["price"],
                            "high": r.get("high") or r["price"], "low": r.get("low") or r["price"],
                            "vol": r.get("vol") or 0}]
            nev = analysis.evaluate(ev["code"], rows, r, session, br["median_pct"], flows, w, sw, s["risk"])
            nev["row"] = r
            return nev
        except (RuntimeError, ValueError, ZeroDivisionError, TypeError):
            return ev
    with ThreadPoolExecutor(max_workers=4) as ex:
        top = list(ex.map(refine, top))
    evaluated = sorted(top + evaluated[30:], key=lambda e: -e["score"])
    # AI 新闻情绪：只给前 8 名做（省调用），正面最多 +4 分、负面最多 -4 分，并写进理由
    try:
        from . import ai
        if ai.ready() and ai.cfg()["auto_sentiment"]:
            STATE["stage"] = "AI 读新闻打情绪分"

            def senti(ev):
                try:
                    ev["sentiment"] = ai.sentiment(ev["code"], ev["row"].get("name"))
                except Exception:  # noqa: BLE001 - AI 失败不影响选股
                    ev["sentiment"] = None
                return ev
            with ThreadPoolExecutor(max_workers=4) as ex:
                top8 = list(ex.map(senti, evaluated[:8]))
            for ev in top8:
                se = ev.get("sentiment")
                if se:
                    ev["score"] = round(ev["score"] + 4 * se["score"], 1)
                    ev["raw"]["sentiment"] = se["score"]
            evaluated = sorted(top8 + evaluated[8:], key=lambda e: -e["score"])
    except Exception:  # noqa: BLE001
        pass

    # 每次都取综合分前 N（默认10）只，再按“门槛”分级：A 重点 / B 关注 / C 观察（宁缺毋滥体现在分级上）
    min_score = cfg["min_score"]
    if reg["code"] == "bear":
        min_score += 5
    elif reg["code"] == "range":
        min_score += 2
    if br["total"] and br["up"] / br["total"] < 0.2:
        min_score += 3
    top_n = int(cfg.get("top_n", 10))
    max_picks = top_n
    picks = []
    try:
        from . import sectors
        sec_data = sectors.compute()
    except Exception:  # noqa: BLE001
        sec_data = {"stock": {}, "by_code": {}}
    per_ind: dict[str, int] = {}
    for ev in evaluated:
        if len(picks) >= top_n:
            break
        if ev["code"] in wl and ev["row"] not in pool:
            continue
        r = ev["row"]
        if "ST" in (r.get("name") or "") or not r.get("price"):
            continue
        lim = market.limit_pct(ev["code"], r.get("name"))
        if r.get("prev") and r["price"] >= round(r["prev"] * (1 + lim) - 0.005, 2):  # 涨停买不进，跳过
            continue
        ic = sec_data["stock"].get(ev["code"])
        if ic and per_ind.get(ic, 0) >= 3:  # 同一行业最多 3 只，分散风险
            continue
        if ic:
            per_ind[ic] = per_ind.get(ic, 0) + 1
        ev["sector"] = sec_data["by_code"].get(ic) if ic else None
        sig = ev["signals"]
        strong = bool(sig) and sig[0]["conf"] >= 0.5 and (ev["plan"]["rr"] or 0) >= 1.4 and ev["ind"]["extra"]["rsi6"] <= 88
        if ev["score"] >= min_score and strong:
            ev["tier"] = "A"
        elif ev["score"] >= min_score - 8 and sig:
            ev["tier"] = "B"
        else:
            ev["tier"] = "C"
        picks.append(ev)

    with db.tx() as c:
        cur = c.execute("INSERT INTO pick_runs(date, session, started, finished, scanned, candidates, picks, market, "
                        "status, weights_ver) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (today(), session, now().strftime("%H:%M:%S"), None, len(snap), len(evaluated), len(picks),
                         db.dumps({"breadth": br, "regime": reg, "min_score": min_score, "max_picks": max_picks}),
                         "done", ver))
        run_id = cur.lastrowid
        c.executemany("INSERT INTO samples(run_id, date, session, code, price, score, factors, strategies) "
                      "VALUES(?,?,?,?,?,?,?,?)",
                      [(run_id, today(), session, e["code"], e["row"]["price"], e["score"], db.dumps(e["raw"]),
                        db.dumps([x["id"] for x in e["signals"]])) for e in evaluated])
        out = []
        for e in picks:
            r = e["row"]
            parts = sorted(e["parts"].items(), key=lambda kv: -kv[1])
            reasons = [x["why"] for x in e["signals"][:3]] + \
                      [f"{LABELS[k]}因子 {v:.0f} 分" for k, v in parts[:3]]
            se = e.get("sentiment")
            if se and se["score"] >= 0.2:
                reasons.append(f"AI 读新闻：偏正面（{se['score']:+.1f}）{se['reason']}")
            sc = e.get("sector")
            if sc and sc["score"] >= 60:
                reasons.append(f"所属行业「{sc['name']}」{sc['tag']}，强度 {sc['score']:.0f}（第 {sc['rank']} 名），领涨 {sc['leader']['name']}")
            risks = [f"{LABELS[k]}偏弱（{v:.0f}分）" for k, v in parts[::-1][:3] if v < 45 and k != "sector"][:2]
            if sc and sc["score"] < 35:
                risks.append(f"所属行业「{sc['name']}」{sc['tag']}（强度 {sc['score']:.0f}），属于逆势个股，注意持续性")
            ex_ = e["ind"]["extra"]
            if ex_["rsi6"] > 75:
                risks.append(f"RSI6={ex_['rsi6']:.0f}，短线偏热，别追高，回落到买入区再分批")
            if se and se["score"] <= -0.2:
                risks.append(f"AI 读新闻：偏负面（{se['score']:+.1f}）{se['reason']}")
            if reg["code"] != "bull":
                risks.append(f"大盘处于{reg['state']}，仓位要比平时轻")
            pl = e["plan"]
            if e["tier"] == "C":
                risks.insert(0, f"仅为观察级：综合分 {e['score']:.0f} 低于门槛 {min_score:.0f} 或缺少可靠买点信号，不建议重仓")
            elif e["tier"] == "B":
                risks.insert(0, f"关注级：离门槛 {min_score:.0f} 分还差一点，适合小仓位试错")
            cur = c.execute("INSERT INTO picks(run_id, date, session, code, name, score, prob, price, stop, target1, "
                            "target2, factors, strategies, reasons, risks, tier, rank, industry) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (run_id, today(), session, e["code"], r.get("name"), e["score"], e["prob"], r["price"],
                             pl["stop"], pl["target1"], pl["target2"], db.dumps(e["parts"]),
                             db.dumps(e["signals"]), db.dumps(reasons), db.dumps(risks), e["tier"],
                             len(out) + 1, sc["name"] if sc else None))
            out.append({"id": cur.lastrowid, "code": e["code"], "name": r.get("name"), "score": e["score"],
                        "industry": sc["name"] if sc else None, "tier": e["tier"], "rank": len(out) + 1,
                        "prob": e["prob"], "price": r["price"], "pct": r.get("pct"), "plan": pl,
                        "signals": e["signals"], "reasons": reasons, "risks": risks})
        c.execute("UPDATE pick_runs SET finished=? WHERE id=?", (now().strftime("%H:%M:%S"), run_id))
    label = {"auction": "竞价选股", "close": "尾盘选股", "manual": "手动选股"}.get(session, session)
    if out:
        na = sum(1 for p in out if p["tier"] == "A")
        body = "\n".join(f"[{p['tier']}] {p['name']}({p['code'][2:]}) 评分{p['score']} 现价{p['price']} 止损{p['plan']['stop']} "
                         f"目标{p['plan']['target1']}" + (f"｜{p['signals'][0]['name']}" if p["signals"] else "") for p in out)
        db.event("pick", f"{label}：前 {len(out)} 名（A级 {na} 只）", body, "good" if na else "info")
    else:
        body = f"扫描 {len(snap)} 只、精评 {len(evaluated)} 只，没有达到门槛（{min_score:.0f}分）的机会。宁缺毋滥，今天先观望。"
        db.event("pick", f"{label}：无符合条件的机会", body)
    notify.send(f"A量化 · {label}", body)
    STATE["last"] = {"run_id": run_id, "picks": len(out), "time": now().strftime("%H:%M:%S"),
                     "secs": round(time.time() - t0, 1)}
    if out and cfg.get("auto_trade", True) and session in ("auction", "close"):
        from . import sim
        try:
            sim.auto_follow(out, session)
        except Exception as e:  # noqa: BLE001
            db.event("system", "AI自动盘跟单失败", str(e)[:200], "warn")
        try:
            from . import plan
            plan.on_picks(out, session)
        except Exception as e:  # noqa: BLE001
            db.event("plan", "计划模拟跟单失败", str(e)[:200], "warn")
    explaining = False
    try:
        from . import ai
        if ai.ready() and ai.cfg()["auto_explain"]:
            ai.explain_run_async(run_id)  # 解读完成后会自动再发布一次
            explaining = True
    except Exception:  # noqa: BLE001
        pass
    if s["publish"].get("enabled") and not explaining:
        from . import publisher
        publisher.publish_async(full=True)
    return {"run_id": run_id, "picks": out, "scanned": len(snap), "candidates": len(evaluated),
            "min_score": min_score, "regime": reg, "breadth": br, "secs": round(time.time() - t0, 1)}


def runs(limit: int = 30) -> list[dict]:
    rs = db.q("SELECT * FROM pick_runs ORDER BY id DESC LIMIT ?", (limit,))
    for r in rs:
        r["market"] = db.loads(r["market"], {})
        r["items"] = picks_of(r["id"])
    return rs


def picks_of(run_id: int) -> list[dict]:
    ps = db.q("SELECT p.*, s.industry FROM picks p LEFT JOIN stock_sector s ON s.code=p.code WHERE run_id=? "
              "ORDER BY score DESC", (run_id,))
    for p in ps:
        for k in ("factors", "strategies", "reasons", "risks"):
            p[k] = db.loads(p[k], [])
    return ps


def top_candidates(run_id: int, n: int = 20) -> list[dict]:
    rows = db.q("SELECT s.code, u.name, s.price, s.score, s.strategies FROM samples s LEFT JOIN universe u "
                "ON u.code=s.code WHERE run_id=? ORDER BY score DESC LIMIT ?", (run_id, n))
    for r in rows:
        r["strategies"] = db.loads(r["strategies"], [])
    return rows
