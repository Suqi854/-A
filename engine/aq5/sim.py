"""模拟盘：贴近实盘的规则（T+1、100股一手、涨跌停买卖不了、佣金/印花税/过户费、滑点）。

两个账户：
- me：我的模拟盘，手动下单，可选“到价自动止损/止盈”。
- ai：AI 自动盘，自动跟随每次选股买入，并严格按交易计划止盈止损，用来积累实战数据。
"""
from __future__ import annotations

import math
from datetime import date

from . import db, market
from .config import load_settings, now, today

ACCOUNTS = {"me": "我的模拟盘", "ai": "AI自动盘"}
EMOTIONS = ["逻辑驱动", "计划内", "跟随系统", "追涨", "抄底", "恐慌", "贪心", "跟风"]


def ensure_accounts() -> None:
    init = load_settings()["init_cash"]
    with db.tx() as c:
        for k, v in ACCOUNTS.items():
            c.execute("INSERT OR IGNORE INTO accounts(id, name, cash, init_cash, created, peak) VALUES(?,?,?,?,?,?)",
                      (k, v, init, init, today(), init))


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


def available(acct: str, code: str) -> int:
    p = db.q1("SELECT qty FROM positions WHERE account=? AND code=?", (acct, code))
    return max(0, (p["qty"] if p else 0) - _bought_today(acct, code))


def can_trade_now() -> bool:
    return market.market_phase() == "trading" and market.is_trading_day()


def place(acct: str, code: str, side: str, qty: int, kind: str = "market", price: float | None = None,
          stop: float | None = None, target1: float | None = None, target2: float | None = None,
          emotion: str = "", reason: str = "", pick_id: int | None = None, auto: bool = True) -> dict:
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
    qd = market.quotes([code]).get(code)
    if not qd or not qd.get("price"):
        raise ValueError("取不到该股票行情")
    if side == "sell":
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


def _execute(o: dict, fill: float) -> str:
    acct, code, qty = o["account"], o["code"], o["qty"]
    amount = fill * qty
    fee = fees(o["side"], amount)
    ts = now().strftime("%Y-%m-%d %H:%M:%S")
    with db.tx() as c:
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
        if not p["auto"]:
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


def auto_follow(picks: list[dict], session: str) -> None:
    ensure_accounts()
    v = view("ai", light=True)
    pct = load_settings()["pick"]["position_pct"]
    for p in picks:
        if db.q1("SELECT 1 FROM positions WHERE account='ai' AND code=?", (p["code"],)):
            continue
        budget = min(v["equity"] * pct, v["cash"] * 0.98)
        qty = int(budget / (p["price"] * 1.003) / 100) * 100
        if qty < 100:
            db.event("trade", f"AI自动盘资金不足，跳过 {p['name']}", "")
            continue
        pl = p["plan"]
        try:
            place("ai", p["code"], "buy", qty, stop=pl["stop"], target1=pl["target1"], target2=pl["target2"],
                  reason=f"{'竞价' if session == 'auction' else '尾盘'}选股：{p['signals'][0]['name']}，评分{p['score']}",
                  emotion="跟随系统", pick_id=p["id"])
            v["cash"] -= qty * p["price"]
        except ValueError as e:
            db.event("trade", f"AI自动盘下单失败 {p['name']}", str(e), "warn")


def view(acct: str, light: bool = False) -> dict:
    ensure_accounts()
    a = db.q1("SELECT * FROM accounts WHERE id=?", (acct,))
    poss = db.q("SELECT * FROM positions WHERE account=? ORDER BY opened", (acct,))
    qs = market.quotes([p["code"] for p in poss]) if poss else {}
    mv, today_pnl = 0.0, 0.0
    for p in poss:
        qd = qs.get(p["code"]) or {}
        px = qd.get("price") or p["cost"]
        p["price"], p["pct"] = px, qd.get("pct")
        p["value"] = round(px * p["qty"], 2)
        p["pnl"] = round((px - p["cost"]) * p["qty"], 2)
        p["pnl_pct"] = round((px / p["cost"] - 1) * 100, 2) if p["cost"] else 0
        p["avail"] = available(acct, p["code"])
        p["hold_days"] = trading_days_between(p["opened"][:10], today())
        p["to_stop"] = round((p["stop"] / px - 1) * 100, 2) if p["stop"] else None
        p["to_target"] = round((p["target1"] / px - 1) * 100, 2) if p["target1"] else None
        if qd.get("prev"):
            bought_today = _bought_today(acct, p["code"])
            old = p["qty"] - bought_today
            today_pnl += (px - qd["prev"]) * old
        mv += p["value"]
    equity = a["cash"] + mv
    out = {"id": acct, "name": a["name"], "cash": round(a["cash"], 2), "market_value": round(mv, 2),
           "equity": round(equity, 2), "init": a["init_cash"], "total_pnl": round(equity - a["init_cash"], 2),
           "total_pct": round((equity / a["init_cash"] - 1) * 100, 2), "today_pnl": round(today_pnl, 2),
           "positions": poss}
    if light:
        return out
    out["orders"] = db.q("SELECT * FROM orders WHERE account=? ORDER BY id DESC LIMIT 50", (acct,))
    trades = db.q("SELECT * FROM trades WHERE account=? ORDER BY id DESC LIMIT 200", (acct,))
    out["trades"] = trades
    out["stats"] = stats(acct, trades)
    out["equity_curve"] = db.q("SELECT date, equity FROM equity WHERE account=? ORDER BY date", (acct,))
    return out


def stats(acct: str, trades=None) -> dict:
    trades = trades or db.q("SELECT * FROM trades WHERE account=?", (acct,))
    sells = [t for t in trades if t["side"] == "sell" and t["pnl"] is not None]
    wins = [t["pnl"] for t in sells if t["pnl"] > 0]
    loss = [t["pnl"] for t in sells if t["pnl"] <= 0]
    emo = {}
    for t in sells:
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
    return {"sells": len(sells), "win_rate": round(len(wins) / len(sells), 3) if sells else None,
            "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
            "avg_loss": round(sum(loss) / len(loss), 2) if loss else None,
            "profit_factor": round(sum(wins) / -sum(loss), 2) if loss and sum(loss) < 0 else None,
            "max_dd": round(mdd * 100, 2), "fees": round(sum(t["fee"] or 0 for t in trades), 2),
            "avg_hold": round(sum(t["hold_days"] or 0 for t in sells) / len(sells), 1) if sells else None,
            "by_emotion": emo}


def record_equity() -> None:
    for k in ACCOUNTS:
        v = view(k, light=True)
        with db.tx() as c:
            c.execute("INSERT OR REPLACE INTO equity VALUES(?,?,?)", (k, today(), v["equity"]))


def reset(acct: str) -> None:
    init = load_settings()["init_cash"]
    with db.tx() as c:
        for t in ("positions", "orders", "trades", "equity"):
            c.execute(f"DELETE FROM {t} WHERE account=?", (acct,))
        c.execute("UPDATE accounts SET cash=?, init_cash=?, peak=? WHERE id=?", (init, init, init, acct))
    db.event("trade", f"{ACCOUNTS[acct]} 已重置为 {init:.0f} 元", "")
