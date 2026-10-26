"""计划模拟：用户设定初始资金、开局买入的股票、已有持仓，AI 根据这些初始条件和行情自己选择策略交易。

账户 plan 按真实 A 股规则（T+1、100股一手、不融资融券），和「我的模拟盘」「AI自动盘」互不影响。

AI 的策略选择（每天开盘后、大盘立场变化、每次选股前都会重新判断）：
  - 防守减仓：大盘风险（risk_off / 防守），或一半以上持仓评分 < 50 → 不开新仓，尾盘减掉最弱的持仓，现金提到一半以上
  - 持仓做T：仓位重（现金 < 25%）且有持仓 → 不追新股，按日内做T信号高抛低吸，降低持仓成本
  - 进攻跟随：现金 ≥ 60% → 跟随竞价/尾盘选股，按 A/B 级分批建仓
  - 波段轮动：其余情况 → 持有强势股，尾盘把转弱（评分 < 50）的持仓换成更强的候选
某个策略在本计划里已平仓 ≥ 5 笔且胜率 < 35% 时，自动降级到下一个更稳的策略（实战反馈）。
"""
from __future__ import annotations

import threading

from . import db, market, sim
from .config import now, today

ACCT = "plan"
MODES = {
    "attack": ("进攻跟随", "现金充足：跟随竞价/尾盘选股，按 A/B 级分批建仓"),
    "rotate": ("波段轮动", "有仓有钱：持有强势股，尾盘把转弱的持仓换成更强的候选"),
    "t_trade": ("持仓做T", "仓位重、现金少：不追新股，用日内高抛低吸降低持仓成本"),
    "defense": ("防守减仓", "大盘有风险或持仓普遍转弱：不开新仓，减掉最弱的，现金提到一半以上"),
}
FALLBACK = {"attack": "rotate", "rotate": "t_trade", "t_trade": "defense", "defense": "defense"}
RISK = {  # 单票仓位、最多持仓只数
    "稳健": {"pos_pct": 0.12, "max_pos": 5},
    "均衡": {"pos_pct": 0.18, "max_pos": 6},
    "积极": {"pos_pct": 0.25, "max_pos": 8},
}
_lock = threading.RLock()


def _row() -> dict | None:
    r = db.q1("SELECT * FROM plan WHERE id=1")
    if r:
        r["cfg"] = db.loads(r["cfg"], {})
        r["history"] = db.loads(r["history"], [])
    return r


def active() -> bool:
    r = _row()
    return bool(r and r["active"] and db.q1("SELECT 1 FROM accounts WHERE id=?", (ACCT,)))


def _tag(mode: str) -> str:
    return f"计划·{MODES[mode][0]}"


# ----------------------------------------------------------------------------- 建立计划
def _clean_items(items, keys) -> list[dict]:
    out = []
    for it in items or []:
        code = str((it or {}).get("code") or "").strip()
        if not code:
            continue
        d = {"code": market.norm_code(code)}
        for k in keys:
            try:
                d[k] = float(it.get(k) or 0)
            except (TypeError, ValueError):
                d[k] = 0.0
        out.append(d)
    return out


def start(b: dict) -> dict:
    """b: {cash, buys:[{code, amount}], holdings:[{code, qty, cost}], risk, mode('auto' 或某个策略)}"""
    cash = float(b.get("cash") or 0)
    if cash < 0:
        raise ValueError("初始资金不能为负")
    buys = _clean_items(b.get("buys"), ("amount",))
    holds = _clean_items(b.get("holdings"), ("qty", "cost"))
    if cash <= 0 and not holds:
        raise ValueError("至少要有初始资金或初始持仓")
    risk = b.get("risk") if b.get("risk") in RISK else "均衡"
    lock = b.get("mode") if b.get("mode") in MODES else "auto"
    for h in holds:
        h["qty"] = int(h["qty"])
        if h["qty"] <= 0:
            raise ValueError(f"{h['code']} 的持仓数量要大于 0")
    if sum(x["amount"] for x in buys) > cash + 1e-6:
        raise ValueError("开局买入的金额合计超过了初始资金")
    codes = list({x["code"] for x in buys + holds})
    qs = market.quotes(codes) if codes else {}
    for x in buys + holds:
        q = qs.get(x["code"]) or {}
        if not q.get("price"):
            raise ValueError(f"取不到 {x['code']} 的行情，请检查代码")
        x["name"] = q.get("name") or x["code"]
        x["price"] = q["price"]
    for h in holds:
        if h["cost"] <= 0:
            h["cost"] = h["price"]
    hold_val = sum(h["qty"] * h["cost"] for h in holds)
    ts = now().strftime("%Y-%m-%d %H:%M:%S")
    cfg = {"cash": cash, "buys": buys, "holdings": holds, "risk": risk, "mode": lock, "init_total": round(cash + hold_val, 2),
           "started": ts}
    with _lock, db.tx() as c:
        for t in ("positions", "orders", "trades", "equity"):
            c.execute(f"DELETE FROM {t} WHERE account=?", (ACCT,))
        c.execute("DELETE FROM accounts WHERE id=?", (ACCT,))
        c.execute("INSERT INTO accounts(id, name, cash, init_cash, created, peak) VALUES(?,?,?,?,?,?)",
                  (ACCT, sim.ACCOUNTS[ACCT], cash, cash + hold_val, today(), cash + hold_val))
        for h in holds:
            c.execute("INSERT INTO positions(account, code, name, qty, cost, opened, last_buy, source, high_since, auto, stage) "
                      "VALUES(?,?,?,?,?,?,?,?,?,1,0)", (ACCT, h["code"], h["name"], h["qty"], h["cost"], ts, ts, "初始持仓", h["price"]))
        c.execute("INSERT OR REPLACE INTO plan(id, created, cfg, mode, reason, updated, stance, history, active) "
                  "VALUES(1,?,?,?,?,?,?,?,1)", (ts, db.dumps(cfg), None, "", ts, None, "[]"))
    for x in buys:
        qty = int(x["amount"] / (x["price"] * 1.003) / 100) * 100
        if qty < 100:
            db.event("plan", f"计划模拟：{x['name']} 金额不够一手，跳过", f"计划 {x['amount']:.0f} 元，现价 {x['price']:.2f}", "warn")
            continue
        try:
            sim.place(ACCT, x["code"], "buy", qty, reason="计划·开局买入", emotion="计划内")
        except ValueError as e:
            db.event("plan", f"计划模拟：开局买入 {x['name']} 失败", str(e), "warn")
    db.event("plan", "计划模拟已建立",
             f"初始资金 {cash:.0f} 元，开局买入 {len(buys)} 只，已有持仓 {len(holds)} 只，风格「{risk}」，"
             f"策略{'由 AI 自选' if lock == 'auto' else '锁定为「' + MODES[lock][0] + '」'}", "good")
    threading.Thread(target=_after_start, daemon=True, name="aq5-plan-start").start()
    return {"ok": True, "message": "计划已建立：AI 正在分析持仓、选择策略" +
            ("（非交易时间的开局买入单会在开盘后自动成交）" if not sim.can_trade_now() else "")}


def _after_start() -> None:
    _set_exits()
    try:
        choose(force=True)
    except Exception as e:  # noqa: BLE001
        db.event("plan", "计划模拟：策略选择出错", str(e)[:200], "warn")


def _set_exits() -> None:
    """给没有止损止盈的持仓补上系统计划价位（初始持仓、开局买入）。"""
    from .analysis import analyze
    for p in db.q("SELECT code, stop FROM positions WHERE account=?", (ACCT,)):
        if p["stop"]:
            continue
        try:
            pl = analyze(p["code"])["plan"]
        except (RuntimeError, ValueError):
            continue
        sim.set_exit(ACCT, p["code"], pl["stop"], pl["target1"], pl["target2"])


def stop() -> dict:
    with db.tx() as c:
        c.execute("UPDATE plan SET active=0 WHERE id=1")
        c.execute("UPDATE orders SET status='cancelled' WHERE account=? AND status='pending'", (ACCT,))
    db.event("plan", "计划模拟已暂停", "持仓和记录保留，AI 不再交易（到价止损止盈也停了）")
    with db.tx() as c:
        c.execute("UPDATE positions SET auto=0 WHERE account=?", (ACCT,))
    return {"ok": True}


def resume() -> dict:
    with db.tx() as c:
        c.execute("UPDATE plan SET active=1 WHERE id=1")
        c.execute("UPDATE positions SET auto=1 WHERE account=?", (ACCT,))
    db.event("plan", "计划模拟已恢复", "")
    threading.Thread(target=lambda: choose(force=True), daemon=True).start()
    return {"ok": True}


def set_mode(mode: str) -> dict:
    r = _row()
    if not r:
        raise ValueError("还没有建立计划")
    cfg = r["cfg"]
    cfg["mode"] = mode if mode in MODES else "auto"
    with db.tx() as c:
        c.execute("UPDATE plan SET cfg=? WHERE id=1", (db.dumps(cfg),))
    return choose(force=True)


# ----------------------------------------------------------------------------- 策略选择
def mode_stats() -> dict:
    """本计划里每个策略的已平仓战绩（按成交理由里的「计划·xxx」标签归类）。"""
    out = {}
    for k, (name, _) in MODES.items():
        rows = db.q("SELECT pnl FROM trades WHERE account=? AND side='sell' AND pnl IS NOT NULL AND reason LIKE ?",
                    (ACCT, f"计划·{name}%"))
        n = len(rows)
        out[k] = {"name": name, "n": n, "win": round(sum(1 for r in rows if r["pnl"] > 0) / n, 3) if n else None,
                  "pnl": round(sum(r["pnl"] for r in rows), 2)}
    return out


def _scores(codes) -> dict:
    from .analysis import analyze
    out = {}
    for c in codes:
        try:
            out[c] = analyze(c)["score"]
        except (RuntimeError, ValueError):
            pass
    return out


def choose(force: bool = False) -> dict:
    """根据现金比例、持仓强弱、大盘立场、本计划各策略战绩，选出当前策略。"""
    r = _row()
    if not r or not r["active"]:
        return {"mode": None}
    from . import marketstate
    try:
        ms = marketstate.state()
    except Exception:  # noqa: BLE001
        ms = {}
    stance = ms.get("stance") or "中性"
    if not force and r["mode"] and r["stance"] == stance and (r["updated"] or "")[:10] == today():
        return {"mode": r["mode"], "reason": r["reason"]}
    v = sim.view(ACCT, light=True)
    eq = v["equity"] or 1
    cash_ratio = max(0.0, v["cash"]) / eq
    sc = _scores([p["code"] for p in v["positions"]])
    weak = [c for c, s in sc.items() if s < 50]
    n = len(v["positions"])
    why = [f"现金占比 {cash_ratio:.0%}", f"持仓 {n} 只" + (f"（{len(weak)} 只评分<50）" if weak else ""), f"大盘「{stance}」"]
    lock = r["cfg"].get("mode", "auto")
    if lock in MODES:
        mode = lock
        why.insert(0, "你锁定了策略")
    elif ms.get("risk_off") or stance == "防守" or (n and len(weak) * 2 >= n and stance in ("谨慎", "防守")):
        mode = "defense"
    elif n and cash_ratio < 0.25:
        mode = "t_trade"
    elif cash_ratio >= 0.6 and stance != "谨慎":
        mode = "attack"
    else:
        mode = "rotate"
    if r["cfg"].get("risk") == "稳健" and mode == "attack" and stance not in ("顺势做多",):
        mode = "rotate"
        why.append("风格稳健：大盘不够强时不全力进攻")
    if lock not in MODES:
        st = mode_stats()
        seen = set()
        while st[mode]["n"] >= 5 and (st[mode]["win"] or 0) < 0.35 and mode not in seen:
            seen.add(mode)
            nxt = FALLBACK[mode]
            why.append(f"「{MODES[mode][0]}」本计划已平仓 {st[mode]['n']} 笔、胜率 {st[mode]['win']:.0%}，降级为「{MODES[nxt][0]}」")
            mode = nxt
    reason = "；".join(why) + f" → {MODES[mode][0]}：{MODES[mode][1]}"
    ts = now().strftime("%Y-%m-%d %H:%M:%S")
    hist = r["history"]
    if mode != r["mode"]:
        hist = (hist + [{"time": ts, "from": r["mode"], "to": mode, "reason": reason}])[-60:]
        db.event("plan", f"计划模拟策略：{MODES[r['mode']][0] if r['mode'] else '开始'} → {MODES[mode][0]}", reason, "info")
    with db.tx() as c:
        c.execute("UPDATE plan SET mode=?, reason=?, updated=?, stance=?, history=? WHERE id=1",
                  (mode, reason, ts, stance, db.dumps(hist)))
    return {"mode": mode, "reason": reason}


# ----------------------------------------------------------------------------- 执行
def _risk() -> dict:
    r = _row()
    return RISK.get((r or {}).get("cfg", {}).get("risk"), RISK["均衡"])


def _sell(code: str, qty: int, why: str, mode: str) -> bool:
    try:
        o = sim.place(ACCT, code, "sell", qty, reason=f"{_tag(mode)}：{why}", emotion="计划内")
        return o["status"] in ("filled", "pending")
    except ValueError as e:
        db.event("plan", f"计划模拟卖出失败 {code}", str(e), "warn")
        return False


def _buy(p: dict, budget: float, why: str, mode: str) -> bool:
    px = p.get("price")
    if not px:
        return False
    qty = int(budget / (px * 1.003) / 100) * 100
    if qty < 100:
        return False
    pl = p.get("plan") or {}
    try:
        sim.place(ACCT, p["code"], "buy", qty, stop=pl.get("stop"), target1=pl.get("target1"), target2=pl.get("target2"),
                  reason=f"{_tag(mode)}：{why}", emotion="计划内", pick_id=p.get("id"))
        return True
    except ValueError as e:
        db.event("plan", f"计划模拟买入失败 {p.get('name')}", str(e), "warn")
        return False


def on_picks(picks: list[dict], session: str) -> None:
    """竞价/尾盘选股后调用：按当前策略决定买卖。"""
    if not active():
        return
    with _lock:
        mode = choose(force=True)["mode"]
        rk = _risk()
        v = sim.view(ACCT, light=True)
        held = {p["code"] for p in v["positions"]}
        pend = {o["code"] for o in db.q("SELECT code FROM orders WHERE account=? AND status='pending'", (ACCT,))}
        eq, cash = v["equity"], v["cash"]
        tag = "竞价" if session == "auction" else "尾盘"
        sc = _scores([p["code"] for p in v["positions"]]) if session == "close" and mode in ("rotate", "defense") else {}
        if mode == "defense" and session == "close" and v["positions"] and cash / max(eq, 1) < 0.5:
            pos = min((p for p in v["positions"] if p["avail"] > 0), key=lambda p: sc.get(p["code"], 50), default=None)
            if pos:
                q = pos["avail"] if sc.get(pos["code"], 50) < 45 else max(100, pos["avail"] // 200 * 100)
                _sell(pos["code"], q, f"减掉最弱的持仓（评分 {sc.get(pos['code'], 0):.0f}），把现金提到一半以上", mode)
            return
        if mode in ("defense", "t_trade"):
            return
        if mode == "rotate" and session == "close":
            for pos in v["positions"]:
                s = sc.get(pos["code"])
                if s is not None and s < 50 and pos["avail"] > 0:
                    if _sell(pos["code"], pos["avail"], f"持仓评分降到 {s:.0f}（<50），换入更强的候选", mode):
                        cash += pos["avail"] * (pos["price"] or pos["cost"]) * 0.998
                        held.discard(pos["code"])
        tiers = ("A", "B") if mode == "attack" else ("A",)
        limit = 3 if mode == "attack" else 2
        bought = 0
        for p in picks:
            if bought >= limit or len(held) >= rk["max_pos"]:
                break
            if p.get("tier", "A") not in tiers or p["code"] in held or p["code"] in pend:
                continue
            k = sim.TIER_SIZE.get(p.get("tier", "A"), 0.4)
            budget = min(eq * rk["pos_pct"] * k, cash * 0.98)
            sig = p["signals"][0]["name"] if p.get("signals") else "综合评分"
            if _buy(p, budget, f"{tag}选股 {p.get('tier', '?')} 级第 {p.get('rank', '?')} 名，{sig}，评分 {p['score']}", mode):
                held.add(p["code"])
                cash -= budget
                bought += 1


def tick() -> None:
    """盘中每 2 分钟：大盘立场变了就重新选策略；做T / 轮动 / 进攻模式下对持仓按做T信号高抛低吸。"""
    if not active() or not sim.can_trade_now():
        return
    from . import signals
    with _lock:
        mode = choose()["mode"]
        if mode not in ("t_trade", "rotate", "attack"):
            return
        hm = now().hour * 100 + now().minute
        for p in db.q("SELECT * FROM positions WHERE account=?", (ACCT,)):
            code = p["code"]
            sold = _t_qty(code, "sell")
            back = _t_qty(code, "buy")
            if hm >= 1450:
                if mode == "t_trade" and sold > back:  # 做T收尾：把高抛的接回，保持持股数量
                    _t_order(code, "buy", sold - back, "做T收尾：尾盘接回高抛的仓位", p, mode)
                continue
            try:
                lv = signals.levels(code)
            except Exception:  # noqa: BLE001
                continue
            fired = signals.check_t(code, lv, p["name"], record=False)
            sides = {f["side"] for f in fired}
            names = "、".join(signals.T_STRATS[f["strategy"]][0] for f in fired)
            if "sell" in sides and not sold:
                q = int(sim.available(ACCT, code) / 3 / 100) * 100
                if q >= 100:
                    _t_order(code, "sell", q, f"做T高抛：{names}（价 {lv['price']}，高抛位 {lv['t_high']}）", p, mode)
            elif "buy" in sides and sold > back:
                _t_order(code, "buy", sold - back, f"做T低吸接回：{names}（价 {lv['price']}，低吸位 {lv['t_low']}）", p, mode)


def _t_qty(code: str, side: str) -> int:
    return int(db.q1("SELECT COALESCE(SUM(qty),0) AS q FROM orders WHERE account=? AND code=? AND side=? AND reason LIKE ? "
                     "AND status IN ('filled','pending') AND created LIKE ?",
                     (ACCT, code, side, "计划·%做T%", today() + "%"))["q"])


def _t_order(code, side, qty, why, pos, mode) -> None:
    try:
        sim.place(ACCT, code, side, qty, reason=f"{_tag(mode)}：{why}", emotion="计划内",
                  stop=pos.get("stop") if side == "buy" else None, target1=pos.get("target1") if side == "buy" else None,
                  target2=pos.get("target2") if side == "buy" else None)
    except ValueError as e:
        db.event("plan", f"计划模拟做T失败 {pos.get('name') or code}", str(e), "warn")


# ----------------------------------------------------------------------------- 展示
def baseline(cfg: dict) -> dict | None:
    """「什么都不做」的对照：初始现金 + 初始持仓 + 开局买入的股票一直拿着，按现价估值。"""
    holds = {h["code"]: h["qty"] for h in cfg.get("holdings", [])}
    spent = 0.0
    for o in db.q("SELECT code, qty, fill_price, fee FROM orders WHERE account=? AND status='filled' AND reason='计划·开局买入'", (ACCT,)):
        holds[o["code"]] = holds.get(o["code"], 0) + o["qty"]
        spent += o["qty"] * o["fill_price"] + (o["fee"] or 0)
    qs = market.quotes(list(holds)) if holds else {}
    mv = 0.0
    for c, q in holds.items():
        px = (qs.get(c) or {}).get("price") or next((h["cost"] for h in cfg.get("holdings", []) if h["code"] == c), 0)
        mv += px * q
    eq = cfg.get("cash", 0) - spent + mv
    init = cfg.get("init_total") or 1
    return {"equity": round(eq, 2), "pct": round((eq / init - 1) * 100, 2)}


def view() -> dict:
    r = _row()
    if not r or not db.q1("SELECT 1 FROM accounts WHERE id=?", (ACCT,)):
        return {"exists": False, "modes": {k: {"name": v[0], "desc": v[1]} for k, v in MODES.items()}, "risks": list(RISK)}
    acct = sim.view(ACCT)
    base = baseline(r["cfg"])
    return {"exists": True, "active": bool(r["active"]), "cfg": r["cfg"], "mode": r["mode"],
            "mode_name": MODES[r["mode"]][0] if r["mode"] else None, "reason": r["reason"], "updated": r["updated"],
            "stance": r["stance"], "history": r["history"][::-1], "stats": mode_stats(), "baseline": base,
            "excess": round(acct["total_pct"] - base["pct"], 2) if base else None,
            "modes": {k: {"name": v[0], "desc": v[1]} for k, v in MODES.items()}, "risks": list(RISK), "account": acct}
