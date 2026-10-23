"""自选股每日信号：盘前计划 / 盘中实时 / 盘后总结。

每只自选股给出：
- 动作：买入 / 加仓 / 持有 / 做T / 减仓 / 卖出 / 止损 / 观望
- 价位：买入区、突破追入价、低吸价、高抛价、止损、止盈①②、加仓价、减仓价
- 做T：多种日内策略（VWAP 偏离、枢轴点、5 分钟 RSI），触发后记录，收盘统计胜率用于迭代

公式：
  枢轴 P = (H+L+C)/3；R1 = 2P − L；S1 = 2P − H；R2 = P + (H − L)；S2 = P − (H − L)（用昨日高低收）
  日内波幅 ATRd ≈ 0.5·ATR14
  VWAP = Σ(价×量)/Σ量（分时均价线）
  低吸价 = VWAP − 0.45·ATR14（S1 落在区间内则取 S1）；高抛价 = VWAP + 0.45·ATR14（R1 落在区间内则取 R1）
  5 分钟 RSI6 < 30 视为超卖、> 70 视为超买
"""
from __future__ import annotations

import time

from . import db, market
from .config import load_settings, now, today

SCHEMA = """
CREATE TABLE IF NOT EXISTS watch_signals(code TEXT, date TEXT, phase TEXT, time TEXT, data TEXT,
  PRIMARY KEY(code, date, phase));
CREATE TABLE IF NOT EXISTS t_signals(id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, time TEXT, code TEXT, name TEXT,
  side TEXT, strategy TEXT, price REAL, close REAL, best REAL, ret REAL, outcome TEXT, account TEXT);
CREATE INDEX IF NOT EXISTS ix_tsig ON t_signals(date, code, strategy);
"""
T_STRATS = {
    "vwap_low": ("VWAP偏离低吸", "buy", "价格 ≤ VWAP − 0.45·ATR14：跌到均价线下方一个“正常波幅”，日内回归均价的概率较大"),
    "pivot_s1": ("枢轴S1支撑", "buy", "价格 ≤ S1 = 2P − H：触及昨日高低收算出的第一支撑"),
    "rsi5_low": ("5分钟RSI超卖", "buy", "5 分钟 RSI6 < 30 且价格低于 VWAP：短线超卖"),
    "vwap_high": ("VWAP偏离高抛", "sell", "价格 ≥ VWAP + 0.45·ATR14：冲到均价线上方一个“正常波幅”"),
    "pivot_r1": ("枢轴R1压力", "sell", "价格 ≥ R1 = 2P − L：触及第一压力位"),
    "rsi5_high": ("5分钟RSI超买", "sell", "5 分钟 RSI6 > 70 且价格高于 VWAP：短线超买"),
}
ACTION_TONE = {"买入": "up", "加仓": "up", "做T": "amber", "持有": "neutral", "观望": "neutral", "减仓": "down",
               "卖出": "down", "止损": "down"}
_fired: dict[str, float] = {}


def init() -> None:
    with db.tx() as c:
        c.executescript(SCHEMA)


def _r(x, n=2):
    return None if x is None else round(x, n)


def _rsi(c, n=6):
    if len(c) < n + 2:
        return None
    up = dn = 0.0
    for i in range(1, len(c)):
        d = c[i] - c[i - 1]
        u, w = max(d, 0), max(-d, 0)
        if i <= n:
            up += u / n
            dn += w / n
        else:
            up = (up * (n - 1) + u) / n
            dn = (dn * (n - 1) + w) / n
    return 100.0 if dn == 0 else 100 - 100 / (1 + up / dn)


def _position(code: str, acct: str = "me") -> dict | None:
    p = db.q1("SELECT * FROM positions WHERE account=? AND code=?", (acct, code))
    if not p:
        return None
    from .sim import available
    p["avail"] = available(acct, code)
    return p


def levels(code: str, a: dict | None = None) -> dict:
    """计算当日关键价位（盘前用昨收，盘中用 VWAP）。"""
    code = market.norm_code(code)
    rows = market.kline(code, 60)
    if rows and rows[-1]["date"] == today():
        rows = rows[:-1]
    if len(rows) < 20:
        raise ValueError("K线不足")
    y = rows[-1]
    P = (y["high"] + y["low"] + y["close"]) / 3
    R1, S1 = 2 * P - y["low"], 2 * P - y["high"]
    R2, S2 = P + (y["high"] - y["low"]), P - (y["high"] - y["low"])
    trs = [max(r["high"] - r["low"], abs(r["high"] - rows[i - 1]["close"]), abs(r["low"] - rows[i - 1]["close"]))
           for i, r in enumerate(rows) if i]
    atr = sum(trs[-14:]) / min(14, len(trs))
    q = market.quotes([code]).get(code) or {}
    price = q.get("price") or y["close"]
    live = market.market_phase() in ("trading", "noon", "close_auction", "after") and str(q.get("time", ""))[:8] == now().strftime("%Y%m%d")
    vwap, rsi5 = None, None
    if live:
        try:
            pts = market.intraday(code).get("points") or []
            if pts:
                vwap = pts[-1].get("avg")
        except RuntimeError:
            pass
        try:
            m5 = market.kline_period(code, "m5", 48).get("rows") or []
            rsi5 = _rsi([r["close"] for r in m5])
        except RuntimeError:
            pass
    anchor = vwap or y["close"]
    low, high = anchor - 0.45 * atr, anchor + 0.45 * atr
    if low < S1 < anchor:      # 支撑在区间内就用支撑（更可靠）
        low = S1
    if anchor < R1 < high:
        high = R1
    if high < low * 1.006:
        mid = (high + low) / 2
        low, high = mid * 0.997, mid * 1.003
    return {"price": price, "prev": y["close"], "pivot": _r(P), "r1": _r(R1), "s1": _r(S1), "r2": _r(R2), "s2": _r(S2),
            "atr": _r(atr, 3), "vwap": _r(vwap), "rsi5": _r(rsi5, 1), "t_low": _r(low), "t_high": _r(high),
            "live": live, "quote": q}


def plan(code: str, phase: str | None = None, acct: str = "me", save: bool = True) -> dict:
    """生成一只股票的当日信号。phase: pre 盘前 / intra 盘中 / post 盘后（默认按当前时间）。"""
    from .analysis import analyze
    code = market.norm_code(code)
    if phase is None:
        ph = market.market_phase()
        phase = "pre" if ph in ("closed", "before", "auction", "pre") and now().hour < 12 else \
            "post" if ph in ("after", "closed") else "intra"
    a = analyze(code)
    lv = levels(code, a)
    pl, mf = a["plan"], a["main_force"]
    price = lv["price"]
    pos = _position(code, acct)
    sigs, actions = [], []

    def add(kind, label, price_, why, tone="neutral", strength=50):
        sigs.append({"kind": kind, "label": label, "price": _r(price_), "why": why, "tone": tone, "strength": strength})

    score, rating = a["score"], a["rating"]
    bad_mf = mf.get("tone") == "down"
    # —— 进出场 ——
    if pos:
        cost = pos["cost"]
        pnl = (price / cost - 1) * 100
        stop = max(pos.get("stop") or 0, pl["stop"])
        if price <= stop:
            actions.append("止损")
            add("stop", "止损离场", stop, f"现价 {price:.2f} 已跌破止损 {stop:.2f}，按纪律离场", "down", 95)
        else:
            add("stop", "止损位", stop, f"收盘跌破 {stop:.2f} 就走（距现价 {(stop / price - 1) * 100:.1f}%）", "down", 60)
        t1 = pos.get("target1") or pl["target1"]
        if t1 and price >= t1:
            actions.append("减仓")
            add("reduce", "到止盈①减半", t1, f"已到第一止盈 {t1:.2f}（浮盈 {pnl:+.1f}%），卖出一半，剩余止损上移到成本 {cost:.2f}", "down", 85)
        else:
            add("take", "止盈①", t1, f"到 {t1:.2f} 先卖一半", "up", 55)
        add("take", "止盈②", pos.get("target2") or pl["target2"], "到这里清仓或跌破5日线清仓", "up", 50)
        if score < 45 or bad_mf:
            actions.append("减仓")
            add("reduce", "转弱减仓", price, f"评分 {score:.0f}" + (f"，主力「{mf['label']}」" if bad_mf else "") + "：先减一半，反弹无力再清仓", "down", 75)
        sup = (pl.get("supports") or [None])[0]
        if score >= 60 and not bad_mf and sup and pnl > -4 and a["tone"] in ("buy", "watch_buy", "hold"):
            add("add", "回踩加仓", sup, f"趋势未坏（评分 {score:.0f}），回踩支撑 {sup:.2f} 附近可加 1/3 仓，止损不变", "up", 60)
            if price <= sup * 1.01:
                actions.append("加仓")
        if not actions:
            actions.append("持有")
    else:
        bz = pl["buy_zone"]
        if a["tone"] in ("buy", "watch_buy") and not bad_mf:
            if bz[0] <= price <= bz[1] * 1.005:
                actions.append("买入")
                add("entry", "买入区内", price, f"评级「{rating}」，价格在买入区 {bz[0]:.2f}~{bz[1]:.2f}，可按计划分批买（{'半仓' if a['tone'] == 'watch_buy' else '计划仓位'}）", "up", 80)
            else:
                add("entry", "等回踩买入", bz[0], f"评级「{rating}」但现价不在买入区，回踩到 {bz[0]:.2f}~{bz[1]:.2f} 再买，不追高", "up", 60)
            res = (pl.get("resistances") or [None])[0]
            if res:
                add("entry", "放量突破追入", res, f"放量（量比>1.5）站上压力 {res:.2f} 可小仓追入，止损放在突破位下方", "up", 50)
        else:
            add("entry", "观望", None, f"评级「{rating}」（{score:.0f}分）" + (f"，主力「{mf['label']}」" if bad_mf else "") + "：暂不买，等评分≥68 或出现买点信号", "neutral", 40)
        add("stop", "若买入的止损", pl["stop"], f"买入后收盘跌破 {pl['stop']:.2f} 离场", "down", 40)
        add("take", "目标①/②", pl["target1"], f"{pl['target1']:.2f} / {pl['target2']:.2f}", "up", 40)
        if not actions:
            actions.append("观望")
    # —— 做T ——
    t_low, t_high = lv["t_low"], lv["t_high"]
    can_sell = bool(pos and pos.get("avail", 0) > 0)
    t_why = (f"今日做T区间：低吸 {t_low} / 高抛 {t_high}（枢轴 S1 {lv['s1']} / R1 {lv['r1']}"
             + (f"，VWAP {lv['vwap']}" if lv["vwap"] else "，盘前按昨收估算") + "）")
    if can_sell:
        add("t_high", "做T高抛", t_high, f"冲到 {t_high} 附近先卖 1/3 可卖仓位，回落到 {t_low} 附近买回（T+1：只能卖昨天以前买的）", "down", 55)
        add("t_low", "做T低吸", t_low, f"回落到 {t_low} 附近接回；跌破止损则不接", "up", 55)
    elif pos:
        add("t_low", "做T低吸（今日新仓）", t_low, "今天新买的不能卖，只能低吸摊低成本", "up", 40)
    else:
        add("t_low", "日内低吸参考", t_low, "没有持仓：可作为日内更好的买点参考", "up", 35)
    triggered = []
    if lv["live"]:
        triggered = check_t(code, lv, a.get("name"), record=False)
        for t in triggered:
            if (t["side"] == "sell" and can_sell) or t["side"] == "buy":
                add("t_fire", "⚡" + T_STRATS[t["strategy"]][0], t["price"], T_STRATS[t["strategy"]][2], "amber", 90)
                if pos and "做T" not in actions and "止损" not in actions:  # 只有持仓才把做T当主动作
                    actions.insert(0, "做T")
    main_action = actions[0]
    res = {"code": code, "name": a["name"], "phase": phase, "time": now().strftime("%Y-%m-%d %H:%M:%S"),
           "action": main_action, "actions": actions, "tone": ACTION_TONE.get(main_action, "neutral"),
           "score": a["score"], "rating": a["rating"], "main_force": mf["label"], "price": _r(price),
           "pct": lv["quote"].get("pct"), "levels": {k: lv[k] for k in ("pivot", "r1", "s1", "r2", "s2", "atr", "vwap", "rsi5",
                                                                       "t_low", "t_high")} | {"buy_zone": pl["buy_zone"],
                                                                                              "stop": _r(max(pos.get("stop") or 0, pl["stop"])) if pos else pl["stop"],
                                                                                              "target1": _r(pos.get("target1") or pl["target1"]) if pos else pl["target1"],
                                                                                              "target2": _r(pos.get("target2") or pl["target2"]) if pos else pl["target2"]},
           "position": {"qty": pos["qty"], "avail": pos["avail"], "cost": _r(pos["cost"], 3),
                        "pnl_pct": _r((price / pos["cost"] - 1) * 100)} if pos else None,
           "signals": sigs, "t_note": t_why, "triggered": triggered, "summary": _summary(main_action, a, pos, price)}
    if save:
        with db.tx() as c:
            c.execute("INSERT OR REPLACE INTO watch_signals VALUES(?,?,?,?,?)",
                      (code, today(), phase, now().strftime("%H:%M:%S"), db.dumps(res)))
    return res


def _summary(action, a, pos, price):
    base = {"买入": "条件满足，可以按计划买入", "加仓": "回踩到支撑、趋势未坏，可小幅加仓", "持有": "按计划持有，守住止损",
            "做T": "盘中触发做T信号，按区间高抛低吸", "减仓": "到达止盈或转弱，先减一半", "卖出": "走势转坏，卖出",
            "止损": "已破止损，按纪律离场", "观望": "没有优势，先观望"}.get(action, "")
    return f"{base}（评分 {a['score']:.0f}，{a['rating']}，主力「{a['main_force']['label']}」）"


def check_t(code: str, lv: dict, name: str | None = None, record: bool = True, account: str = "watch") -> list[dict]:
    """检查日内做T触发；record=True 时记入 t_signals（同一股票同一策略 30 分钟内只记一次）。"""
    price, vwap, atr, rsi5 = lv["price"], lv["vwap"], lv["atr"], lv["rsi5"]
    if not price or not vwap:
        return []
    out = []
    conds = {
        "vwap_low": price <= vwap - 0.45 * atr,
        "pivot_s1": price <= lv["s1"],
        "rsi5_low": rsi5 is not None and rsi5 < 30 and price < vwap,
        "vwap_high": price >= vwap + 0.45 * atr,
        "pivot_r1": price >= lv["r1"],
        "rsi5_high": rsi5 is not None and rsi5 > 70 and price > vwap,
    }
    for k, hit in conds.items():
        if not hit:
            continue
        sig = {"strategy": k, "side": T_STRATS[k][1], "price": _r(price), "time": now().strftime("%H:%M")}
        out.append(sig)
        key = f"{today()}:{code}:{k}:{account}"
        if record and time.time() - _fired.get(key, 0) > 1800:
            _fired[key] = time.time()
            with db.tx() as c:
                c.execute("INSERT INTO t_signals(date, time, code, name, side, strategy, price, account) VALUES(?,?,?,?,?,?,?,?)",
                          (today(), now().strftime("%H:%M:%S"), code, name, sig["side"], k, price, account))
    return out


def evaluate_t() -> int:
    """收盘后：评估当天 T 信号是否有效。低吸：之后日内最高 ≥ +0.8% 或收盘 > 信号价 +0.3% 算成功；高抛相反。"""
    rows = db.q("SELECT * FROM t_signals WHERE date=? AND outcome IS NULL", (today(),))
    n = 0
    for r in rows:
        try:
            pts = market.intraday(r["code"]).get("points") or []
        except RuntimeError:
            continue
        hh = r["time"][:5]
        after = [p["p"] for p in pts if p["t"] >= hh] or [r["price"]]
        close = after[-1]
        if r["side"] == "buy":
            best = max(after)
            ok = best >= r["price"] * 1.008 or close >= r["price"] * 1.003
            ret = (close / r["price"] - 1) * 100
        else:
            best = min(after)
            ok = best <= r["price"] * 0.992 or close <= r["price"] * 0.997
            ret = (r["price"] / close - 1) * 100
        with db.tx() as c:
            c.execute("UPDATE t_signals SET close=?, best=?, ret=?, outcome=? WHERE id=?",
                      (close, best, round(ret, 3), "win" if ok else "loss", r["id"]))
        n += 1
    return n


def t_stats(days: int = 60) -> list[dict]:
    rows = db.q("SELECT strategy, outcome, ret FROM t_signals WHERE outcome IS NOT NULL AND date >= date(?, ?)",
                (today(), f"-{days} day"))
    out = []
    for k, (name, side, desc) in T_STRATS.items():
        xs = [r for r in rows if r["strategy"] == k]
        n = len(xs)
        win = sum(1 for r in xs if r["outcome"] == "win") / n if n else None
        avg = sum(r["ret"] or 0 for r in xs) / n if n else None
        out.append({"id": k, "name": name, "side": side, "desc": desc, "n": n, "win": _r(win, 3), "avg": _r(avg, 3),
                    "weight": _r(max(0.5, min(1.5, 0.5 + (win or 0.5) + (avg or 0) / 2)) if n >= 10 else 1.0, 2)})
    return out


def run_watch(phase: str | None = None) -> dict:
    """给全部自选股生成信号（盘前 9:05、盘中每 30 分钟、盘后 15:10）。"""
    wl = [market.norm_code(c) for c in load_settings()["watchlist"]]
    ok, err = 0, []
    for code in wl:
        try:
            plan(code, phase)
            ok += 1
        except Exception as e:  # noqa: BLE001
            err.append(f"{code}: {e}")
    return {"ok": ok, "errors": err[:5]}


def scan_t() -> int:
    """盘中每 2 分钟：自选股 + AI 自动盘持仓 检查做T触发，记录用于统计；AI 盘按信号自动做T。"""
    wl = {market.norm_code(c) for c in load_settings()["watchlist"]}
    ai_pos = {p["code"]: p for p in db.q("SELECT * FROM positions WHERE account='ai'")}
    n = 0
    for code in wl | set(ai_pos):
        try:
            lv = levels(code)
        except Exception:  # noqa: BLE001
            continue
        acct = "ai" if code in ai_pos else "watch"
        fired = check_t(code, lv, (lv["quote"] or {}).get("name"), record=True, account=acct)
        n += len(fired)
        if acct == "ai" and fired:
            _ai_t(code, ai_pos[code], fired, lv)
    return n


def _ai_t(code: str, pos: dict, fired: list[dict], lv: dict) -> None:
    """AI 自动盘做T：高抛卖 1/3 可卖仓位；之后低吸买回同样数量（同一天最多各一次）。"""
    from .sim import available, place
    sells = db.q1("SELECT COALESCE(SUM(qty),0) AS q FROM orders WHERE account='ai' AND code=? AND side='sell' AND reason LIKE '做T%' "
                  "AND created LIKE ?", (code, today() + "%"))["q"]
    buys = db.q1("SELECT COALESCE(SUM(qty),0) AS q FROM orders WHERE account='ai' AND code=? AND side='buy' AND reason LIKE '做T%' "
                 "AND created LIKE ?", (code, today() + "%"))["q"]
    sides = {f["side"] for f in fired}
    names = "、".join(T_STRATS[f["strategy"]][0] for f in fired)
    try:
        if "sell" in sides and not sells:
            q = int(available("ai", code) / 3 / 100) * 100
            if q >= 100:
                place("ai", code, "sell", q, reason=f"做T高抛：{names}（价 {lv['price']}，高抛位 {lv['t_high']}）", emotion="跟随系统")
        elif "buy" in sides and sells and not buys:
            place("ai", code, "buy", int(sells), reason=f"做T低吸买回：{names}（价 {lv['price']}，低吸位 {lv['t_low']}）",
                  emotion="跟随系统", stop=pos.get("stop"), target1=pos.get("target1"), target2=pos.get("target2"))
    except ValueError as e:
        db.event("trade", f"AI自动盘做T失败 {code}", str(e), "warn")


def latest(code: str | None = None) -> list[dict] | dict | None:
    if code:
        r = db.q1("SELECT data FROM watch_signals WHERE code=? ORDER BY date DESC, "
                  "CASE phase WHEN 'post' THEN 3 WHEN 'intra' THEN 2 ELSE 1 END DESC, time DESC LIMIT 1",
                  (market.norm_code(code),))
        return db.loads(r["data"], None) if r else None
    rows = db.q("SELECT code, data FROM watch_signals WHERE date=(SELECT MAX(date) FROM watch_signals) ORDER BY time DESC")
    seen, out = set(), []
    for r in rows:
        if r["code"] not in seen:
            seen.add(r["code"])
            d = db.loads(r["data"], {})
            out.append({k: d.get(k) for k in ("code", "name", "phase", "time", "action", "tone", "score", "rating", "main_force")})
    return out


def phases(code: str) -> dict:
    rows = db.q("SELECT phase, data FROM watch_signals WHERE code=? AND date=(SELECT MAX(date) FROM watch_signals WHERE code=?)",
                (market.norm_code(code), market.norm_code(code)))
    return {r["phase"]: db.loads(r["data"], {}) for r in rows}
