"""模拟盘：贴近实盘的规则（T+1、100股一手、涨跌停买卖不了、佣金/印花税/过户费、滑点）。

两个账户：
- me：我的模拟盘，手动下单，可选“到价自动止损/止盈”。
- ai：AI 自动盘，自动跟随每次选股买入，并严格按交易计划止盈止损，用来积累实战数据。
"""
from __future__ import annotations

import math
from datetime import date

from . import accounting, db, market
from .config import load_settings, now, today

ACCOUNTS = {"me": "我的模拟盘", "ai": "AI自动盘", "plan": "计划模拟盘"}
AUTO_ACCOUNTS = ("me", "ai")  # 启动时自动建好的账户；计划模拟盘由用户设定后才创建
EMOTIONS = ["逻辑驱动", "计划内", "跟随系统", "追涨", "抄底", "恐慌", "贪心", "跟风"]


def ensure_accounts() -> None:
    init = load_settings()["init_cash"]
    with db.tx() as c:
        accounting.init(c)
        for k in AUTO_ACCOUNTS:
            v = ACCOUNTS[k]
            c.execute("INSERT OR IGNORE INTO accounts(id, name, cash, init_cash, created, peak) VALUES(?,?,?,?,?,?)",
                      (k, v, init, init, today(), init))


def migrate_capital() -> None:
    """把模拟资金提到设置里的 init_cash（默认 100 万）：没交易过的账户直接重置，有交易的只补资金、保留记录。"""
    target = float(load_settings()["init_cash"])
    for a in db.q("SELECT * FROM accounts"):
        if a["id"] not in AUTO_ACCOUNTS or a["init_cash"] >= target:
            continue
        n = (db.q1("SELECT COUNT(*) AS n FROM orders WHERE account=?", (a["id"],)) or {}).get("n", 0)
        if not n:
            reset(a["id"])
            continue
        delta = target - a["init_cash"]
        with db.tx() as c:
            updated = c.execute(
                "UPDATE accounts SET cash=cash+?, init_cash=?, peak=COALESCE(peak,0)+? WHERE id=? AND init_cash=?",
                (delta, target, delta, a["id"], a["init_cash"]),
            )
            if updated.rowcount != 1:
                continue
            accounting.record_cash_flow(c, a["id"], today(), "funding", delta)
        db.event("trade", f"{ACCOUNTS[a['id']]} 资金已提升到 {target / 1e4:.0f} 万", f"补入 {delta:.0f} 元，原有持仓和记录保留")


def existing() -> list[str]:
    return [r["id"] for r in db.q("SELECT id FROM accounts") if r["id"] in ACCOUNTS]


def fees(side: str, amount: float) -> float:
    f = load_settings()["fees"]
    fee = max(amount * f["commission"], f["min_commission"]) + amount * f["transfer"]
    if side == "sell":
        fee += amount * f["stamp_tax"]
    return round(fee, 2)


def _bought_today(acct: str, code: str) -> int:
    r = db.q1("SELECT COALESCE(SUM(qty),0) n FROM trades WHERE account=? AND code=? AND side='buy' AND time LIKE ?",
              (acct, code, today() + "%"))
    return int(r["n"])


def ai_rules() -> dict:
    """AI 自动盘的模拟规则（可在设置里改）：T+0、融资、融券、杠杆。我的模拟盘始终按真实 A 股规则。"""
    r = {"t0": True, "margin": True, "short": True, "leverage": 2.0, "margin_rate": 0.06, "short_rate": 0.08,
         "maint": 1.3}
    r.update(load_settings().get("ai_rules", {}))
    return r


def _rules_on(acct: str) -> bool:
    return acct == "ai"


def available(acct: str, code: str) -> int:
    p = db.q1("SELECT qty FROM positions WHERE account=? AND code=?", (acct, code))
    q = p["qty"] if p else 0
    if _rules_on(acct) and ai_rules()["t0"]:
        return max(0, q)          # AI 盘 T+0：当天买的当天能卖
    return max(0, q - _bought_today(acct, code))


def exposure(acct: str) -> dict:
    """多头市值、空头市值、融资负债、维持担保比例。"""
    a = db.q1("SELECT cash FROM accounts WHERE id=?", (acct,))
    poss = db.q("SELECT code, qty, cost FROM positions WHERE account=?", (acct,))
    qs = market.quotes_recent([p["code"] for p in poss]) if poss else {}
    lmv = sum((qs.get(p["code"], {}).get("price") or p["cost"]) * p["qty"] for p in poss if p["qty"] > 0)
    smv = sum((qs.get(p["code"], {}).get("price") or p["cost"]) * -p["qty"] for p in poss if p["qty"] < 0)
    cash = a["cash"] if a else 0
    equity = cash + lmv - smv
    borrowed = max(0.0, -cash)
    liab = borrowed + smv
    assets = max(0.0, cash) + lmv
    return {"long_mv": round(lmv, 2), "short_mv": round(smv, 2), "borrowed": round(borrowed, 2), "equity": round(equity, 2),
            "gross": round(lmv + smv, 2), "leverage": round((lmv + smv) / equity, 2) if equity > 0 else None,
            "maint_ratio": round(assets / liab, 3) if liab > 0 else None}


def can_trade_now() -> bool:
    return market.market_phase() == "trading" and market.is_trading_day()


def place(acct: str, code: str, side: str, qty: int, kind: str = "market", price: float | None = None,
          stop: float | None = None, target1: float | None = None, target2: float | None = None,
          emotion: str = "", reason: str = "", pick_id: int | None = None, auto: bool = True, qd: dict | None = None) -> dict:
    code = market.norm_code(code)
    if acct not in ACCOUNTS:
        raise ValueError("账户不存在")
    if side not in ("buy", "sell"):
        raise ValueError("方向错误")
    qty = int(qty)
    if qty <= 0:
        raise ValueError("数量必须大于0")
    if side == "buy" and qty % 100:
        raise ValueError("买入必须是100股（1手）的整数倍")
    qd = qd or (market.quotes_fast if acct == "ai" else market.quotes)([code]).get(code)
    if not qd or not qd.get("price"):
        raise ValueError("取不到该股票行情")
    if _rules_on(acct):
        r = ai_rules()
        ex = exposure(acct)
        cur = db.q1("SELECT qty FROM positions WHERE account=? AND code=?", (acct, code))
        cq = cur["qty"] if cur else 0
        new_q = cq + qty if side == "buy" else cq - qty
        if new_q < 0 and not r["short"]:
            raise ValueError("未开启融券，不能卖出超过持仓")
        add_gross = max(0, abs(new_q) - abs(cq)) * qd["price"]
        if add_gross > 0 and ex["equity"] > 0 and ex["gross"] + add_gross > r["leverage"] * ex["equity"] + 1:
            raise ValueError(f"超过杠杆上限 {r['leverage']} 倍（当前总敞口 {ex['gross'] / 1e4:.0f} 万，净资产 {ex['equity'] / 1e4:.0f} 万）")
    elif side == "sell":
        av = available(acct, code)
        if qty > av:
            raise ValueError(f"可卖数量只有 {av} 股（A股T+1：今天买的明天才能卖）")
    if kind == "limit" and not price:
        raise ValueError("限价单需要填写价格")
    with db.tx() as c:
        cur = c.execute("INSERT INTO orders(account, code, name, side, kind, price, qty, status, created, updated, "
                        "reason, emotion, pick_id, stop, target1, target2, auto) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (acct, code, qd.get("name"), side, kind, price, qty, "pending",
                         now().strftime("%Y-%m-%d %H:%M:%S"), now().strftime("%Y-%m-%d %H:%M:%S"), reason, emotion,
                         pick_id, stop, target1, target2, 1 if auto else 0))
        oid = cur.lastrowid
    res = try_fill(oid, qd)
    o = db.q1("SELECT * FROM orders WHERE id=?", (oid,))
    o["message"] = res
    return o


def try_fill(oid: int, qd: dict | None = None) -> str:
    o = db.q1("SELECT * FROM orders WHERE id=?", (oid,))
    if not o or o["status"] != "pending":
        return "订单不是待成交状态"
    if not can_trade_now():
        return "非连续竞价时间，订单已挂起，开盘后按规则自动成交"
    qd = qd or market.quotes([o["code"]]).get(o["code"])
    if not qd or not qd.get("price") or qd.get("suspended"):
        return "停牌或无行情，暂不成交"
    px = qd["price"]
    slip = load_settings()["slippage"]
    up, dn = qd.get("up_limit"), qd.get("dn_limit")
    if o["side"] == "buy":
        if up and px >= up - 0.001:
            return "涨停价无法买入（排队中）"
        if o["kind"] == "limit" and px > o["price"]:
            return "未到限价"
        fill = round(min(px * (1 + slip), o["price"]) if o["kind"] == "limit" else px * (1 + slip), 2)
    else:
        if dn and px <= dn + 0.001:
            return "跌停价无法卖出（排队中）"
        if o["kind"] == "limit" and px < o["price"]:
            return "未到限价"
        fill = round(max(px * (1 - slip), o["price"]) if o["kind"] == "limit" else px * (1 - slip), 2)
    return _execute(o, fill)


def _execute_ai(o: dict, fill: float) -> str:
    """AI 盘：持仓数量可正（多头）可负（空头）；现金可为负（融资）。"""
    acct, code, qty = o["account"], o["code"], o["qty"]
    amount = fill * qty
    fee = fees(o["side"], amount)
    ts = now().strftime("%Y-%m-%d %H:%M:%S")
    r = ai_rules()
    with db.tx() as c:
        c.execute("BEGIN IMMEDIATE")
        state = c.execute("SELECT status FROM orders WHERE id=?", (o["id"],)).fetchone()
        if state is None or state["status"] != "pending":
            return "订单已处理"
        a = c.execute("SELECT cash FROM accounts WHERE id=?", (acct,)).fetchone()
        pos = c.execute("SELECT * FROM positions WHERE account=? AND code=?", (acct, code)).fetchone()
        old = pos["qty"] if pos else 0
        cost = pos["cost"] if pos else fill
        if o["side"] == "buy" and not r["margin"] and amount + fee > a["cash"] + 1e-6:
            c.execute("UPDATE orders SET status='rejected', note='资金不足', updated=? WHERE id=?", (ts, o["id"]))
            return "资金不足，订单作废"
        sign = 1 if o["side"] == "buy" else -1
        new = old + sign * qty
        pnl = None
        closing = min(qty, abs(old)) if old and (old > 0) != (sign > 0) else 0
        if closing:
            pnl = round(((fill - cost) if old > 0 else (cost - fill)) * closing - fee, 2)
        if new == 0:
            c.execute("DELETE FROM positions WHERE account=? AND code=?", (acct, code))
        elif pos and (old > 0) == (new > 0) and closing == 0:      # 同方向加仓：加权成本
            ncost = (cost * abs(old) + fill * qty) / abs(new)
            c.execute("UPDATE positions SET qty=?, cost=?, last_buy=?, stop=COALESCE(?,stop), target1=COALESCE(?,target1), "
                      "target2=COALESCE(?,target2) WHERE account=? AND code=?",
                      (new, ncost, ts, o["stop"], o["target1"], o["target2"], acct, code))
        elif pos and (old > 0) == (new > 0):                         # 部分平仓
            c.execute("UPDATE positions SET qty=? WHERE account=? AND code=?", (new, acct, code))
        else:                                                         # 新开仓或反手
            c.execute("DELETE FROM positions WHERE account=? AND code=?", (acct, code))
            c.execute("INSERT INTO positions(account, code, name, qty, cost, stop, target1, target2, opened, last_buy, source, "
                      "pick_id, high_since, auto, stage) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
                      (acct, code, o["name"], new, fill, o["stop"], o["target1"], o["target2"], ts, ts, o["reason"],
                       o["pick_id"], fill, o["auto"]))
        c.execute("UPDATE accounts SET cash=cash+? WHERE id=?", (-(amount + fee) if sign > 0 else (amount - fee), acct))
        c.execute("UPDATE orders SET status='filled', fill_price=?, fee=?, updated=? WHERE id=?", (fill, fee, ts, o["id"]))
        c.execute("INSERT INTO trades(account, code, name, side, qty, price, fee, time, pnl, order_id, reason, emotion, pick_id, "
                  "hold_days) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (acct, code, o["name"], o["side"], qty, fill, fee, ts, pnl, o["id"], o["reason"], o["emotion"], o["pick_id"],
                   trading_days_between(pos["opened"][:10], today()) if pos and closing else None))
    kind = ("平空" if old < 0 else "买入") if sign > 0 else ("卖出" if old > 0 else "融券卖出")
    msg = f"{ACCOUNTS[acct]} {kind} {o['name']} {qty}股 @ {fill:.2f}" + (f"，盈亏 {pnl:+.2f}" if pnl is not None else "")
    db.event("trade", msg, o["reason"] or "", "good" if (pnl or 0) >= 0 else "warn")
    return "已成交：" + msg


def _execute(o: dict, fill: float) -> str:
    if _rules_on(o["account"]):
        return _execute_ai(o, fill)
    acct, code, qty = o["account"], o["code"], o["qty"]
    amount = fill * qty
    fee = fees(o["side"], amount)
    ts = now().strftime("%Y-%m-%d %H:%M:%S")
    with db.tx() as c:
        c.execute("BEGIN IMMEDIATE")
        current = c.execute("SELECT status FROM orders WHERE id=?", (o["id"],)).fetchone()
        if not current or current["status"] != "pending":
            return "订单不是待成交状态"
        a = c.execute("SELECT cash FROM accounts WHERE id=?", (acct,)).fetchone()
        pos = c.execute("SELECT * FROM positions WHERE account=? AND code=?", (acct, code)).fetchone()
        if o["side"] == "buy":
            if amount + fee > a["cash"] + 1e-6:
                c.execute("UPDATE orders SET status='rejected', note='资金不足', updated=? WHERE id=?", (ts, o["id"]))
                return "资金不足，订单作废"
            c.execute("UPDATE accounts SET cash=cash-? WHERE id=?", (amount + fee, acct))
            if pos:
                nq = pos["qty"] + qty
                ncost = (pos["cost"] * pos["qty"] + amount + fee) / nq
                c.execute("UPDATE positions SET qty=?, cost=?, last_buy=?, stop=COALESCE(?,stop), "
                          "target1=COALESCE(?,target1), target2=COALESCE(?,target2) WHERE account=? AND code=?",
                          (nq, ncost, ts, o["stop"], o["target1"], o["target2"], acct, code))
            else:
                c.execute("INSERT INTO positions(account, code, name, qty, cost, stop, target1, target2, opened, "
                          "last_buy, source, pick_id, high_since, auto, stage) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
                          (acct, code, o["name"], qty, (amount + fee) / qty, o["stop"], o["target1"], o["target2"],
                           ts, ts, o["reason"], o["pick_id"], fill, o["auto"]))
            pnl, hold = None, None
        else:
            if not pos or pos["qty"] < qty:
                c.execute("UPDATE orders SET status='rejected', note='持仓不足', updated=? WHERE id=?", (ts, o["id"]))
                return "持仓不足"
            if qty > pos["qty"] - _bought_today(acct, code):
                c.execute("UPDATE orders SET status='rejected', note='T+1可卖数量不足', updated=? WHERE id=?", (ts, o["id"]))
                return "T+1可卖数量不足，订单作废"
            pnl = round((fill - pos["cost"]) * qty - fee, 2)
            hold = trading_days_between(pos["opened"][:10], today())
            c.execute("UPDATE accounts SET cash=cash+? WHERE id=?", (amount - fee, acct))
            if pos["qty"] == qty:
                c.execute("DELETE FROM positions WHERE account=? AND code=?", (acct, code))
            else:
                c.execute("UPDATE positions SET qty=qty-? WHERE account=? AND code=?", (qty, acct, code))
        c.execute("UPDATE orders SET status='filled', fill_price=?, fee=?, updated=? WHERE id=?",
                  (fill, fee, ts, o["id"]))
        c.execute("INSERT INTO trades(account, code, name, side, qty, price, fee, time, pnl, order_id, reason, "
                  "emotion, pick_id, hold_days) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (acct, code, o["name"], o["side"], qty, fill, fee, ts, pnl, o["id"], o["reason"], o["emotion"],
                   o["pick_id"], hold))
    side = "买入" if o["side"] == "buy" else "卖出"
    msg = f"{ACCOUNTS[acct]} {side} {o['name']} {qty}股 @ {fill:.2f}" + (f"，盈亏 {pnl:+.2f}" if pnl is not None else "")
    db.event("trade", msg, o["reason"] or "", "good" if (pnl or 0) >= 0 else "warn")
    return "已成交：" + msg


def cancel(oid: int) -> None:
    with db.tx() as c:
        c.execute("UPDATE orders SET status='cancelled', updated=? WHERE id=? AND status='pending'",
                  (now().strftime("%Y-%m-%d %H:%M:%S"), oid))


def trading_days_between(d1: str, d2: str) -> int:
    a, b = date.fromisoformat(d1), date.fromisoformat(d2)
    n, cur = 0, a
    while cur < b:
        cur = date.fromordinal(cur.toordinal() + 1)
        if cur.weekday() < 5:
            n += 1
    return n


def set_exit(acct: str, code: str, stop=None, target1=None, target2=None, auto=None) -> None:
    with db.tx() as c:
        c.execute("UPDATE positions SET stop=COALESCE(?,stop), target1=COALESCE(?,target1), "
                  "target2=COALESCE(?,target2), auto=COALESCE(?,auto) WHERE account=? AND code=?",
                  (stop, target1, target2, None if auto is None else int(bool(auto)), acct, code))


def tick() -> None:
    """盘中每隔几秒调用：撮合挂单、检查止损止盈。"""
    if not can_trade_now():
        return
    pend = db.q("SELECT id, code FROM orders WHERE status='pending'")
    poss = db.q("SELECT * FROM positions")
    codes = list({p["code"] for p in pend} | {p["code"] for p in poss})
    if not codes:
        return
    qs = market.quotes(codes)
    for o in pend:
        try_fill(o["id"], qs.get(o["code"]))
    risk = load_settings()["risk"]
    for p in poss:
        qd = qs.get(p["code"])
        if not qd or not qd.get("price"):
            continue
        px = qd["price"]
        if px > (p["high_since"] or 0):
            with db.tx() as c:
                c.execute("UPDATE positions SET high_since=? WHERE account=? AND code=?", (px, p["account"], p["code"]))
        if not p["auto"] or p["qty"] < 0:
            continue
        av = available(p["account"], p["code"])
        if av <= 0:
            continue
        reason = None
        qty = av
        if p["stop"] and px <= p["stop"]:
            reason = f"触发止损 {p['stop']:.2f}"
        elif p["target2"] and px >= p["target2"]:
            reason = f"达到第二止盈 {p['target2']:.2f}"
        elif p["target1"] and px >= p["target1"] and p["stage"] == 0:
            qty = max(100, (av // 200) * 100) if av >= 200 else av
            reason = f"达到第一止盈 {p['target1']:.2f}，先卖一半"
        elif p["account"] == "ai" and trading_days_between(p["opened"][:10], today()) >= risk["max_hold_days"]:
            reason = f"持有满 {risk['max_hold_days']} 个交易日，时间止损"
        if reason:
            o = place(p["account"], p["code"], "sell", qty, reason=reason, emotion="计划内")
            if "第一止盈" in reason and o["status"] == "filled":
                set_exit(p["account"], p["code"], stop=round(max(p["cost"], p["stop"] or 0), 2))
                with db.tx() as c:
                    c.execute("UPDATE positions SET stage=1 WHERE account=? AND code=?", (p["account"], p["code"]))


TIER_SIZE = {"A": 1.0, "B": 0.7, "C": 0.4}


def _traded_today(acct: str) -> int:
    return (db.q1("SELECT COUNT(*) AS n FROM orders WHERE account=? AND created LIKE ? AND status IN ('filled','pending')",
                  (acct, today() + "%")) or {}).get("n", 0)


def auto_follow(picks: list[dict], session: str) -> None:
    """旧版 AI 自动盘的受控兼容入口，仅允许 A/B 级候选。

    不使用未校准的上涨概率进行凯利放大；不为凑数据强制交易。
    """
    if not load_settings().get("strategy_v2", {}).get("legacy_auto", False):
        return
    ensure_accounts()
    cfg = load_settings()["pick"]
    base = float(cfg.get("ai_position_pct", 0.12))
    max_pos = max(1, int(cfg.get("ai_max_positions", 12)))  # 真正的约束是杠杆上限（下单时检查），持仓只数放宽
    v = view("ai", light=True)
    held = {p["code"] for p in v["positions"]}
    pend = {o["code"] for o in db.q("SELECT code FROM orders WHERE account='ai' AND status='pending'")}
    tag = "竞价" if session == "auction" else "尾盘"
    bought = 0

    def buy(p, size_k, why):
        nonlocal bought
        budget = min(v["equity"] * base * size_k, v["cash"] * 0.98)
        qty = int(budget / (p["price"] * 1.003) / 100) * 100
        if qty < 100:
            return False
        pl = p["plan"]
        sig = p["signals"][0]["name"] if p.get("signals") else "综合评分"
        try:
            place("ai", p["code"], "buy", qty, stop=pl["stop"], target1=pl["target1"], target2=pl["target2"],
                  reason=f"{tag}选股[{p.get('tier', '?')}级 第{p.get('rank', '?')}名]：{sig}，评分{p['score']}；{why}",
                  emotion="跟随系统", pick_id=p.get("id"))
        except ValueError as e:
            db.event("trade", f"AI自动盘下单失败 {p['name']}", str(e), "warn")
            return False
        v["cash"] -= qty * p["price"]
        held.add(p["code"])
        bought += 1
        return True

    # 收盘场先做调仓：持有≥2天且评分转弱（<50）的卖出，腾出仓位
    if session == "close":
        from .analysis import analyze
        for pos in v["positions"]:
            if pos["avail"] <= 0 or pos.get("hold_days", 0) < 2:
                continue
            try:
                sc = analyze(pos["code"])["score"]
            except (RuntimeError, ValueError):
                continue
            if sc < 50:
                try:
                    place("ai", pos["code"], "sell", pos["avail"], reason=f"量化调仓：持仓评分降到 {sc:.0f}（<50），换入更强的股票",
                          emotion="跟随系统")
                    v["cash"] += pos["avail"] * (pos["price"] or pos["cost"]) * 0.998
                    held.discard(pos["code"])
                except ValueError:
                    pass
    for p in picks:
        if p.get("tier") not in ("A", "B") or p.get("eligible") is False:
            continue
        if p["code"] in held or p["code"] in pend:
            continue
        if len(held) >= max_pos:
            break
        buy(p, TIER_SIZE[p.get("tier", "A")], f"仓位系数 {TIER_SIZE[p.get('tier', 'A')]}")



def view(acct: str, light: bool = False) -> dict:
    if not db.writes_frozen():
        ensure_accounts()
    day = today()
    a, poss, plan, anchor, fills, flows, latest_fill = accounting.snapshot(acct, day)
    if not a:
        raise ValueError(f"{ACCOUNTS.get(acct, acct)}还没有建立")
    codes = {p["code"] for p in poss}
    codes.update(t["code"] for t in fills if t["code"] and str(t["time"] or "")[:10] == day)
    qs = market.quotes_recent(sorted(codes)) if codes else {}
    asof = now()
    today_pnl, daily_status, daily_basis = accounting.daily_result(
        day, a, poss, plan, anchor, fills, flows, qs, asof=asof, latest_fill=latest_fill,
    )

    def previous_close_allowed():
        return market.market_phase(asof) in ("closed", "before", "auction", "pre") or not market.is_trading_day()

    valuation_status, valuation_asof, valuation_freshness, valuation_time, close_eligible = accounting.valuation_evidence(
        poss, qs, asof, latest_fill, previous_close_allowed,
    )
    mv = 0.0
    for p in poss:
        qd = qs.get(p["code"]) or {}
        px = qd.get("price") or p["cost"]
        p["price"], p["pct"] = px, qd.get("pct")
        p["value"] = round(px * p["qty"], 2)
        p["pnl"] = round((px - p["cost"]) * p["qty"], 2)
        p["side"] = "short" if p["qty"] < 0 else "long"
        p["pnl_pct"] = round((px / p["cost"] - 1) * 100 * (1 if p["qty"] > 0 else -1), 2) if p["cost"] else 0
        p["avail"] = available(acct, p["code"])
        p["hold_days"] = trading_days_between(p["opened"][:10], today())
        p["to_stop"] = round((p["stop"] / px - 1) * 100, 2) if p["stop"] else None
        p["to_target"] = round((p["target1"] / px - 1) * 100, 2) if p["target1"] else None
        mv += p["value"]
    equity = a["cash"] + mv
    init = a["init_cash"]
    total_pnl = equity - init if valuation_asof and init else None
    out = {"id": acct, "name": a["name"], "cash": round(a["cash"], 2), "market_value": round(mv, 2),
           "equity": round(equity, 2), "init": init, "total_pnl": round(total_pnl, 2) if total_pnl is not None else None,
           "total_pct": round(total_pnl / init * 100, 2) if total_pnl is not None else None,
           "today_pnl": today_pnl, "daily_pnl_status": daily_status,
           "performance": {"valuation_status": valuation_status, "daily_basis": daily_basis,
                           "valuation_asof": valuation_asof, "valuation_freshness": valuation_freshness,
                           "valuation_time": valuation_time, "close_eligible": close_eligible,
                           "verified_equity": round(equity, 2) if valuation_asof else None,
                           "cumulative_basis": "recorded_initial_capital", "as_of_date": day},
           "positions": poss}
    if _rules_on(acct):
        out["margin"] = {**exposure(acct), "rules": ai_rules()}
    if light:
        return out
    out["orders"] = db.q("SELECT * FROM orders WHERE account=? ORDER BY id DESC LIMIT 50", (acct,))
    trades = db.q("SELECT * FROM trades WHERE account=? ORDER BY id DESC LIMIT 200", (acct,))
    out["trades"] = trades
    out["stats"] = stats(acct)
    out["equity_curve"] = db.q("SELECT date, equity FROM equity WHERE account=? ORDER BY date", (acct,))
    return out


def stats(acct: str, trades=None) -> dict:
    trades = db.q("SELECT * FROM trades WHERE account=? ORDER BY id", (acct,)) if trades is None else trades
    closes = [t for t in trades if t["side"] in ("buy", "sell") and t["pnl"] is not None]
    wins = [t["pnl"] for t in closes if t["pnl"] > 0]
    loss = [t["pnl"] for t in closes if t["pnl"] < 0]
    sell_closes = sum(t["side"] == "sell" for t in closes)
    emo = {}
    for t in closes:
        e = t["emotion"] or "未标注"
        d = emo.setdefault(e, {"n": 0, "win": 0, "pnl": 0.0})
        d["n"] += 1
        d["win"] += 1 if t["pnl"] > 0 else 0
        d["pnl"] = round(d["pnl"] + t["pnl"], 2)
    curve = [r["equity"] for r in db.q("SELECT equity FROM equity WHERE account=? ORDER BY date", (acct,))]
    mdd, peak = 0.0, -math.inf
    for v in curve:
        peak = max(peak, v)
        mdd = max(mdd, (peak - v) / peak if peak > 0 else 0)
    n = len(closes)
    return {"sells": n, "closed_trades": n, "sell_closes": sell_closes, "short_cover_closes": n - sell_closes,
            "wins": len(wins), "losses": len(loss), "breakeven": n - len(wins) - len(loss),
            "win_rate": round(len(wins) / n, 3) if n else None,
            "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
            "avg_loss": round(sum(loss) / len(loss), 2) if loss else None,
            "profit_factor": round(sum(wins) / -sum(loss), 2) if loss and sum(loss) < 0 else None,
            "max_dd": round(mdd * 100, 2), "max_dd_basis": "recorded_equity_unadjusted",
            "fees": round(sum(t["fee"] or 0 for t in trades), 2),
            "avg_hold": round(sum(t["hold_days"] or 0 for t in closes) / n, 1) if n else None,
            "by_emotion": emo, "cost_note": "历史平仓统计沿用原流水口径，未完整分摊全部开仓费用；费用后表现请看账户权益和新训练两腿净额。"}


def accrue_interest() -> None:
    """收盘后计息：融资负债按年化利率、融券市值按年化费率，按天扣除。"""
    if not ai_rules()["margin"] and not ai_rules()["short"]:
        return
    ex = exposure("ai")
    r = ai_rules()
    cost = ex["borrowed"] * r["margin_rate"] / 365 + ex["short_mv"] * r["short_rate"] / 365
    if cost > 0.01:
        with db.tx() as c:
            day = today()
            legacy_post = c.execute(
                "SELECT 1 FROM events WHERE kind='trade' AND time LIKE ? AND title LIKE 'AI自动盘 两融利息 %' LIMIT 1",
                (day + "%",),
            ).fetchone()
            if legacy_post or not accounting.record_cash_flow(c, "ai", day, "interest", -cost):
                return
            c.execute("UPDATE accounts SET cash=cash-? WHERE id='ai'", (cost,))
        db.event("trade", f"AI自动盘 两融利息 {cost:.2f} 元", f"融资 {ex['borrowed']:.0f} × {r['margin_rate']:.0%}/365 + 融券 {ex['short_mv']:.0f} × {r['short_rate']:.0%}/365")


def check_maintenance() -> None:
    """维持担保比例低于设定（默认 130%）时强制平掉敞口最大的一笔。"""
    ex = exposure("ai")
    r = ai_rules()
    if ex["maint_ratio"] is None or ex["maint_ratio"] >= r["maint"]:
        return
    poss = db.q("SELECT code, qty, cost FROM positions WHERE account='ai'")
    qs = market.quotes([p["code"] for p in poss])
    big = max(poss, key=lambda p: abs(p["qty"]) * (qs.get(p["code"], {}).get("price") or p["cost"]), default=None)
    if big:
        place("ai", big["code"], "buy" if big["qty"] < 0 else "sell", abs(big["qty"]),
              reason=f"强制平仓：维持担保比例 {ex['maint_ratio']:.0%} 低于 {r['maint']:.0%}", emotion="计划内")


def record_equity() -> None:
    t = now()
    if t.weekday() >= 5 or t.hour < 15:
        return
    try:
        accrue_interest()
    except Exception:  # noqa: BLE001
        pass
    for k in existing():
        v = view(k, light=True)
        with db.tx() as c:
            day = v["performance"]["as_of_date"]
            accounting.record_close(c, k, day)
            if v["performance"]["close_eligible"] and v["performance"]["valuation_asof"] == day:
                c.execute("INSERT OR REPLACE INTO equity VALUES(?,?,?)", (k, day, v["performance"]["verified_equity"]))


def reset(acct: str) -> None:
    init = load_settings()["init_cash"]
    with db.tx() as c:
        for t in ("positions", "orders", "trades", "equity"):
            c.execute(f"DELETE FROM {t} WHERE account=?", (acct,))
        accounting.init(c)
        c.execute("DELETE FROM accounting_cash WHERE account=?", (acct,))
        c.execute("UPDATE accounts SET cash=?, init_cash=?, peak=? WHERE id=?", (init, init, init, acct))
    db.event("trade", f"{ACCOUNTS[acct]} 已重置为 {init:.0f} 元", "")
