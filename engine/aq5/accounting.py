"""Evidence and reconciliation for the simulation account views."""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, time, timedelta

from . import db
from .config import CN_TZ

MAX_INTRADAY_QUOTE_AGE = timedelta(seconds=15)


def init(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS accounting_cash(
            id INTEGER PRIMARY KEY AUTOINCREMENT, account TEXT NOT NULL, date TEXT NOT NULL,
            kind TEXT NOT NULL, amount REAL NOT NULL, trade_id INTEGER, quantities TEXT
        )
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS ix_accounting_once
        ON accounting_cash(account, date, kind) WHERE kind IN ('close', 'interest')
    """)


def record_cash_flow(conn, acct: str, day: str, kind: str, amount: float) -> bool:
    """Record a cash movement in the same transaction as the account update."""
    init(conn)
    cursor = conn.execute(
        "INSERT OR IGNORE INTO accounting_cash(account,date,kind,amount) VALUES(?,?,?,?)",
        (acct, day, kind, amount),
    )
    return cursor.rowcount == 1


def record_close(conn, acct: str, day: str) -> None:
    """Freeze the observed ledger state for later cash/inventory reconciliation."""
    if acct == "plan":
        return  # The opening configuration is the ledger anchor for each new plan.
    conn.execute(
        "INSERT OR IGNORE INTO accounting_cash(account,date,kind,amount,trade_id,quantities) "
        "SELECT a.id,?,'close',a.cash, "
        "(SELECT COALESCE(MAX(id),0) FROM trades WHERE account=a.id), "
        "(SELECT json_group_object(code,qty) FROM positions WHERE account=a.id) "
        "FROM accounts a WHERE a.id=? AND a.cash IS NOT NULL",
        (day, acct),
    )


def snapshot(acct: str, day: str):
    """Read one SQLite snapshot; no network requests are made under it."""
    conn = db.conn()
    conn.execute("SAVEPOINT accounting_snapshot")
    try:
        a = db.q1("SELECT * FROM accounts WHERE id=?", (acct,))
        positions = db.q("SELECT * FROM positions WHERE account=? ORDER BY opened", (acct,))
        plan = db.q1("SELECT cfg,created FROM plan WHERE id=1") if acct == "plan" else None
        anchor = db.q1(
            "SELECT * FROM accounting_cash j WHERE account=? AND kind='close' AND date<? "
            "AND (trade_id=0 OR EXISTS(SELECT 1 FROM trades t WHERE t.account=j.account AND t.id=j.trade_id)) "
            "ORDER BY date DESC LIMIT 1",
            (acct, day),
        )
        fills = db.q(
            "SELECT code, side, qty, price, fee, time FROM trades WHERE account=? AND id>? ORDER BY id",
            (acct, anchor["trade_id"] if anchor else 0),
        )
        flows = db.q(
            "SELECT date, kind, amount FROM accounting_cash WHERE account=? AND id>? AND kind!='close'",
            (acct, anchor["id"] if anchor else 0),
        )
        latest_fill = db.q1("SELECT MAX(time) AS time FROM trades WHERE account=?", (acct,))
        return a, positions, plan, anchor, fills, flows, latest_fill["time"]
    finally:
        conn.execute("RELEASE accounting_snapshot")


def status(code: str, reason: str) -> dict:
    return {"code": code, "reason": reason}


def _number(value, *, positive: bool = False) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value) and (not positive or value > 0)


def _fill_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return _cn(datetime.fromisoformat(value))
    except (TypeError, ValueError):
        return None


def _cn(t: datetime) -> datetime:
    return t.astimezone(CN_TZ) if t.tzinfo else t.replace(tzinfo=CN_TZ)


def _quote_time(quote: dict) -> datetime | None:
    if quote.get("suspended") or not _number(quote.get("price"), positive=True):
        return None
    stamp = str(quote.get("time") or "")
    if len(stamp) < 14:
        return None
    try:
        return datetime.strptime(stamp[:14], "%Y%m%d%H%M%S").replace(tzinfo=CN_TZ)
    except ValueError:
        return None


def _session_mark(stamp, asof, filled):
    if (not stamp or stamp.date() != asof.date() or stamp > asof or asof.weekday() >= 5
            or filled and stamp < filled):
        return False
    t, q = asof.time(), stamp.time()
    if time(9, 30) <= t < time(11, 30):
        return q >= time(9, 30) and asof - stamp <= MAX_INTRADAY_QUOTE_AGE
    if time(11, 30) <= t < time(13):
        return time(11, 29, 45) <= q < time(13)
    if time(13) <= t < time(15):
        return q >= time(13) and asof - stamp <= MAX_INTRADAY_QUOTE_AGE
    return t >= time(15) and q >= time(15)


def valuation_evidence(positions, quotes, asof: datetime, latest_fill: str | None, previous_close_allowed):
    asof = _cn(asof)
    day = asof.date().isoformat()
    filled = _fill_time(latest_fill)

    def result(code, reason, stamp=None, freshness="unknown", close_eligible=False):
        return (status(code, reason), stamp.date().isoformat() if stamp else None, freshness,
                stamp.isoformat(timespec="seconds") if stamp else None, close_eligible)

    if latest_fill and (not filled or filled > asof):
        return result("incomplete_trade_time", "最新成交时间缺失或晚于本次估值。")
    at_close = asof.weekday() < 5 and asof.time() >= time(15)
    stamps = [_quote_time(quotes.get(p["code"]) or {}) for p in positions if p["qty"]]
    if not stamps:
        return result("ok", "当前账户全部为现金。", asof, "cash_only", at_close)
    if all(_session_mark(stamp, asof, filled) for stamp in stamps):
        return result("ok", "使用有效会话内且不早于最后成交的行情估值。",
                      min(stamps), "current_session", at_close)
    if all(stamp and stamp.weekday() < 5 and stamp.time() >= time(15) for stamp in stamps):
        dates = {stamp.date().isoformat() for stamp in stamps}
        if len(dates) == 1:
            close_day = dates.pop()
            if close_day < day and (not filled or filled <= min(stamps)) and previous_close_allowed():
                return result("ok", f"按 {close_day} 收盘报价估值。", min(stamps), "prior_close")
    return result("missing_current_mark", "缺少有效会话行情，或行情早于最新成交。")


def _config(plan: dict | None) -> dict:
    cfg = db.loads(plan["cfg"], {}) if plan else {}
    return cfg if isinstance(cfg, dict) else {}


def _holdings(cfg: dict) -> dict | None:
    positions = cfg.get("holdings")
    if not isinstance(positions, list):
        return None
    result = {}
    for p in positions:
        if (not isinstance(p, dict) or not p.get("code") or p["code"] in result
                or not _number(p.get("qty"), positive=True)):
            return None
        result[p["code"]] = p
    return result


def daily_result(day: str, acct: dict, positions: list[dict], plan, anchor, fills, flows, quotes: dict,
                 *, asof: datetime, latest_fill: str | None):
    """Daily equity change from signed opening/current marks and executed cash flows."""
    def unknown(code, reason):
        return None, status(code, reason), None

    asof = _cn(asof)
    filled = _fill_time(latest_fill)
    if day != asof.date().isoformat() or latest_fill and (not filled or filled > asof):
        return unknown("incomplete_trade_time", "账本与最新成交的时点无法核对。")
    cfg = _config(plan)
    holdings = _holdings(cfg) if acct["id"] == "plan" else {}
    if holdings is None:
        return unknown("missing_plan_origin", "缺少当前计划的开局持仓记录。")
    start_cash = (anchor["amount"] if anchor else
                  cfg.get("cash") if acct["id"] == "plan" else acct["init_cash"])
    if not _number(start_cash) or not _number(acct["cash"]):
        return unknown("missing_cash_basis", "缺少可对账的初始现金或最近一次现金快照。")

    quantities = db.loads(anchor["quantities"], {}) if anchor else {code: p["qty"] for code, p in holdings.items()}
    book_quantities = defaultdict(int, quantities)
    net_by_code = defaultdict(int)
    cash = start_cash
    pnl_cash = 0.0
    for fill in fills:
        fill_day = str(fill["time"] or "")[:10]
        if (len(fill_day) != 10 or fill_day > day or not fill["code"] or fill["side"] not in ("buy", "sell")
                or not _number(fill["qty"], positive=True) or not _number(fill["price"], positive=True)
                or not _number(fill["fee"]) or fill["fee"] < 0):
            return unknown("incomplete_trade", "成交账本缺少日期、方向、数量、价格或费用。")
        signed_qty = fill["qty"] if fill["side"] == "buy" else -fill["qty"]
        amount = -signed_qty * fill["price"] - fill["fee"]
        book_quantities[fill["code"]] += signed_qty
        cash += amount
        if fill_day == day:
            net_by_code[fill["code"]] += signed_qty
            pnl_cash += amount

    for flow in flows:
        if flow["date"] > day or not _number(flow["amount"]) or flow["kind"] not in ("interest", "funding"):
            return unknown("incomplete_cash_flow", "现金流水含不明日期、类型或金额。")
        if anchor or flow["kind"] != "funding":
            cash += flow["amount"]  # Without a close, recorded initial capital already includes funding.
        if flow["date"] == day and flow["kind"] == "interest":
            pnl_cash += flow["amount"]

    if abs(cash - acct["cash"]) >= 0.005:
        return unknown("unreconciled_cash", "账户现金与已记录的起点、成交和现金流水无法对账。")
    current = {p["code"]: p["qty"] for p in positions if p["qty"]}
    if {code: qty for code, qty in book_quantities.items() if qty} != current:
        return unknown("unreconciled_positions", "持仓数量与已记录的起点和成交无法对账。")

    plan_day = str((plan or {}).get("created") or cfg.get("started") or "")[:10]
    inception = acct["id"] == "plan" and plan_day == day
    basis = "recorded_plan_inception_mark" if inception else "same_day_quote_previous_reference"
    for code in current.keys() | net_by_code.keys():
        qty = current.get(code, 0)
        opening_qty = qty - net_by_code.get(code, 0)
        quote = quotes.get(code) or {}
        mark_is_current = _session_mark(_quote_time(quote), asof, filled)
        if qty:
            if not mark_is_current:
                return unknown("missing_current_mark", "现有持仓缺少有效会话报价，或报价早于最新成交。")
            pnl_cash += qty * quote["price"]
        if opening_qty:
            if inception:
                opening_price = holdings.get(code, {}).get("price")
                if not _number(opening_price, positive=True):
                    return unknown("missing_plan_mark", "计划起点缺少已记录的持仓行情估值。")
            else:
                opening_price = quote.get("prev")
                if not mark_is_current or not _number(opening_price, positive=True):
                    return unknown("missing_previous_close", "日初持仓缺少当日行情附带的昨收参考价。")
            pnl_cash -= opening_qty * opening_price

    return round(pnl_cash, 2), status("ok", "已按开局或昨收参考价、当日估值、成交及已记账费用核算。"), basis


def plan_performance(cfg: dict, account_view: dict) -> dict:
    """Display historical cost and saved inception marks as distinct baselines."""
    value = account_view.get("equity")
    valuation = account_view.get("performance", {}).get("valuation_status", {})
    verified_value = _number(value) and valuation.get("code") == "ok"
    holdings = _holdings(cfg)
    cash = cfg.get("cash")
    marked = None
    if holdings is not None and _number(cash) and all(_number(p.get("price"), positive=True) for p in holdings.values()):
        marked = cash + sum(p["qty"] * p["price"] for p in holdings.values())

    def summarize(basis, valid_code, valid_reason):
        valid_basis = _number(basis, positive=True)
        if not valid_basis:
            evidence = status("missing_basis", "缺少该口径的开局基准。")
        elif not verified_value:
            evidence = status("missing_current_mark", "账户当前权益缺少可核对的行情估值。")
        else:
            evidence = status(valid_code, valid_reason)
        pnl = value - basis if valid_basis and verified_value else None
        return {"basis": round(basis, 2) if valid_basis else None, "pnl": round(pnl, 2) if pnl is not None else None,
                "return_pct": round(pnl / basis * 100, 2) if pnl is not None else None, "status": evidence}

    return {
        "historical_cost": summarize(cfg.get("init_total"), "recorded_cost", "使用用户录入的历史成本，包含建盘前浮盈浮亏。"),
        "inception_mark": summarize(marked, "recorded_mark", "使用建盘时配置保存的估值，未独立核验报价时间戳。"),
        "holding_comparison": {"status": status("not_frozen", "尚无冻结的逐日持有基准。")},
    }

