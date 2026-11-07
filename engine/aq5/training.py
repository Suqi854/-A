"""Independent, forward-only T practice books fed by the intraday evidence stream."""
from __future__ import annotations

import math
import re
from collections import Counter
from datetime import datetime, time, timedelta

from . import db, market, sim
from .config import CN_TZ, load_settings, now

VERSION = "training-t1"
VARIANTS = {"fast": 4, "balanced": 8, "slow": 12}  # minimum net edge in basis points
MAX_AGE = 12
MAX_CODES = 40
COOLDOWN = timedelta(minutes=3)
ACTIVE = ("awaiting_session", "active")
SCHEMA = """
CREATE TABLE IF NOT EXISTS training_runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, version TEXT NOT NULL, created_at TEXT NOT NULL,
  cfg TEXT NOT NULL, status TEXT NOT NULL, opened_at TEXT, baseline TEXT, last_tick TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS ix_training_active
  ON training_runs((1)) WHERE status IN ('awaiting_session','active');
CREATE TABLE IF NOT EXISTS training_books(
  run_id INTEGER NOT NULL, variant TEXT NOT NULL, mode TEXT NOT NULL, cash INTEGER NOT NULL,
  holdings TEXT NOT NULL, PRIMARY KEY(run_id, variant));
CREATE TABLE IF NOT EXISTS training_cycles(
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL, variant TEXT NOT NULL,
  code TEXT NOT NULL, qty INTEGER NOT NULL, status TEXT NOT NULL, opened_at TEXT NOT NULL,
  sold_fen INTEGER NOT NULL, closed_at TEXT, net_fen INTEGER);
CREATE UNIQUE INDEX IF NOT EXISTS ix_training_open_cycle
  ON training_cycles(run_id,variant,code) WHERE status='open';
CREATE TABLE IF NOT EXISTS training_orders(
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL, variant TEXT NOT NULL,
  code TEXT NOT NULL, side TEXT NOT NULL, qty INTEGER NOT NULL, cycle_id INTEGER,
  opportunity_id TEXT NOT NULL, submitted_at TEXT NOT NULL, signal_quote_time TEXT NOT NULL,
  expires_at TEXT NOT NULL, source TEXT NOT NULL, rules TEXT NOT NULL, status TEXT NOT NULL,
  reason TEXT, updated_at TEXT, fill_quote_time TEXT, fill_price_fen INTEGER, fee_fen INTEGER,
  UNIQUE(run_id,variant,code,opportunity_id));
CREATE UNIQUE INDEX IF NOT EXISTS ix_training_pending
  ON training_orders(run_id,variant,code) WHERE status='pending';
CREATE TABLE IF NOT EXISTS training_observations(
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL, variant TEXT NOT NULL,
  code TEXT NOT NULL, identity TEXT NOT NULL, observed_at TEXT NOT NULL,
  opportunity_id TEXT, phase TEXT, decision TEXT NOT NULL, reason TEXT NOT NULL, evidence TEXT,
  UNIQUE(run_id,variant,code,identity));
CREATE TABLE IF NOT EXISTS training_marks(
  run_id INTEGER NOT NULL, variant TEXT NOT NULL, date TEXT NOT NULL, marked_at TEXT NOT NULL,
  equity_fen INTEGER NOT NULL, hold_fen INTEGER NOT NULL, restore_cash_fen INTEGER NOT NULL,
  open_net_fen INTEGER NOT NULL, evidence TEXT NOT NULL,
  PRIMARY KEY(run_id,variant,date));
"""


def init() -> None:
    with db.tx() as conn:
        conn.executescript(SCHEMA)


def _exists() -> bool:
    return bool(db.q1("SELECT 1 FROM sqlite_master WHERE type='table' AND name='training_runs'"))


def _stamp(value) -> datetime | None:
    try:
        if isinstance(value, str):
            if len(value) == 14 and value.isascii() and value.isdecimal():
                value = datetime.strptime(value, "%Y%m%d%H%M%S")
            else:
                value = datetime.fromisoformat(value)
        if isinstance(value, datetime):
            return value.astimezone(CN_TZ) if value.tzinfo else value.replace(tzinfo=CN_TZ)
    except ValueError:
        pass
    return None


def _asof(value) -> datetime:
    stamp = now() if value is None else _stamp(value)
    if stamp is None:
        raise ValueError("无效训练时间")
    return stamp.replace(microsecond=0)


def _iso(t: datetime) -> str:
    return t.isoformat(timespec="seconds")


def _session(t: datetime) -> datetime | None:
    if t.weekday() < 5:
        for start, end in ((time(9, 30), time(11, 30)), (time(13), time(14, 57))):
            if start <= t.time() < end:
                return datetime.combine(t.date(), end, CN_TZ)
    return None


def _number(x, *, positive=False) -> float | None:
    if isinstance(x, (float, int)) and not isinstance(x, bool) and math.isfinite(x) and (not positive or x > 0):
        return float(x)
    return None


def _fen(value) -> int:
    return round(value * 100)


def _yuan(fen: int | None) -> float | None:
    return fen / 100 if fen is not None else None


def _active(conn):
    return conn.execute("SELECT * FROM training_runs WHERE status IN ('awaiting_session','active')").fetchone()


def _row(conn, sql, args=()):
    item = conn.execute(sql, args).fetchone()
    return dict(item) if item else None


def _opening_cfg(cfg: dict) -> dict:
    cash = _number(cfg.get("cash"))
    if cash is None or cash < 0:
        raise ValueError("训练开局现金需为非负数字")
    holdings = cfg.get("holdings")
    if not isinstance(holdings, list) or not holdings:
        raise ValueError("T 训练需要原计划中至少一只有效的初始持仓")
    if len(holdings) > MAX_CODES:
        raise ValueError(f"同一训练实验最多支持 {MAX_CODES} 只初始持仓")
    quantities = {}
    for item in holdings:
        if not isinstance(item, dict):
            raise ValueError("初始持仓格式错误")
        raw_code = item.get("code")
        code = market.norm_code(raw_code) if isinstance(raw_code, str) else ""
        qty = _number(item.get("qty"), positive=True)
        if not re.fullmatch(r"(sh|sz)\d{6}", code) or not qty or qty % 100 or code in quantities:
            raise ValueError("初始持仓须为沪深标的、100股整数且无重复")
        quantities[code] = int(qty)
    return {"cash_fen": _fen(cash), "quantities": quantities,
            "source_plan_started_at": cfg.get("started"), "ignored_opening_buys": len(cfg.get("buys") or [])}


def start_from_plan(cfg=None, asof=None) -> dict:
    """Register an experiment without replaying the previous account or opening a position."""
    t = _asof(asof)
    if cfg is None:
        row = db.q1("SELECT cfg FROM plan WHERE id=1")
        cfg = db.loads(row["cfg"], {}) if row else None
    if not isinstance(cfg, dict):
        raise ValueError("没有可用的原计划开局配置")
    origin = _opening_cfg(cfg)
    if not _exists():
        init()
    with db.tx() as conn:
        conn.execute("BEGIN IMMEDIATE")
        run = _active(conn)
        if run:
            if db.loads(run["cfg"]) != origin:
                raise ValueError("已有活动训练实验，不能覆盖其冻结基准")
            return {"run_id": run["id"], "status": run["status"], "existing": True}
        cur = conn.execute("INSERT INTO training_runs(version,created_at,cfg,status) VALUES(?,?,?,'awaiting_session')",
                           (VERSION, _iso(t), db.dumps(origin)))
        run_id = cur.lastrowid
        conn.executemany("INSERT INTO training_books VALUES(?,?,'shadow',?,?)",
                         ((run_id, variant, origin["cash_fen"], "{}") for variant in VARIANTS))
    return {"run_id": run_id, "status": "awaiting_session", "existing": False}


def arm_paper(variants) -> dict:
    """Explicitly opt pending books into paper fills once real opening evidence arrives."""
    if isinstance(variants, str):
        variants = [variants]
    selected = set(variants or ())
    if not selected <= VARIANTS.keys():
        raise ValueError("未知训练版本")
    if not _exists():
        raise ValueError("请先建立训练实验")
    with db.tx() as conn:
        conn.execute("BEGIN IMMEDIATE")
        run = _active(conn)
        if not run:
            raise ValueError("请先建立训练实验")
        existing = {x["variant"] for x in conn.execute("SELECT variant FROM training_books WHERE run_id=? AND mode='paper'",
                                                        (run["id"],))}
        if run["status"] != "awaiting_session" and existing != selected:
            raise ValueError("实验已开局，不能混合影子观测与模拟成交账本")
        if run["status"] == "awaiting_session":
            conn.executemany("UPDATE training_books SET mode=? WHERE run_id=? AND variant=?",
                             (("paper" if v in selected else "shadow", run["id"], v) for v in VARIANTS))
    return {"run_id": run["id"], "status": run["status"], "paper_variants": sorted(selected)}


def codes() -> list[str]:
    if not _exists():
        return []
    run = _active(db.conn())
    return list(db.loads(run["cfg"])["quantities"]) if run else []


def _quote(raw, t, end) -> tuple[dict | None, str | None]:
    if market.MOCK:
        return None, "mock_feed_disabled"
    if not isinstance(raw, dict):
        return None, "missing_data"
    stamp = _stamp(raw.get("quote_time") or raw.get("time"))
    received = _stamp(raw.get("received_at")) if raw.get("received_at") else t
    price = _number(raw.get("price"), positive=True)
    source = raw.get("source")
    if not stamp or not received or not price or not isinstance(source, str) or not source.strip():
        return None, "missing_data"
    if raw.get("suspended"):
        return None, "suspended"
    if _session(stamp) != end or stamp > received or received > t or (t - stamp).total_seconds() > MAX_AGE:
        return None, "stale_data"
    return {**raw, "price": price, "stamp": _iso(stamp), "source": source.strip()}, None


def _signal_issue(sig, quote, t, code, variant) -> str | None:
    from .intraday import VERSION as signal_version

    if not isinstance(sig, dict) or sig.get("version") != signal_version or sig.get("code") != code or sig.get("variant") != variant:
        return "missing_data"
    evaluated = _stamp(sig.get("evaluated_at"))
    if not evaluated or evaluated > t or (t - evaluated).total_seconds() > MAX_AGE:
        return "stale_data"
    features = sig.get("features")
    if not isinstance(features, dict):
        return "missing_data"
    backdrop = features.get("market")
    if not isinstance(backdrop, dict):
        backdrop = {}
    if backdrop.get("risk_off") is True or sig.get("reason") == "market_risk_off":
        return "market_defense"
    if sig.get("setup") == "risk_exit":
        return "risk_exit"
    if sig.get("phase") == "invalid":
        return "signal_invalid"
    if backdrop.get("risk_off") is not False or sig.get("phase") == "unknown" and sig.get("missing"):
        return "missing_data"
    if not quote:
        return "missing_data"
    signal_stamp = _stamp(sig.get("quote_time"))
    if not signal_stamp or _iso(signal_stamp) != quote["stamp"] or features.get("source") != quote["source"]:
        return "stale_data"
    return None


def _observe(conn, run_id, variant, code, t, sig, decision, reason, *, order_id=None):
    sig = sig or {}
    op = sig.get("opportunity_id")
    revision = sig.get("revision")
    # Transition ids are preserved. Coverage gaps are sampled once per half-minute.
    interval = _iso(t.replace(second=30 if t.second >= 30 else 0))
    origin = f"{op}:{revision}" if op else interval
    identity = f"{origin}:{decision}:{reason}:{order_id or ''}"
    conn.execute(
        "INSERT OR IGNORE INTO training_observations(run_id,variant,code,identity,observed_at,"
        "opportunity_id,phase,decision,reason,evidence) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (run_id, variant, code, identity, _iso(t), op, sig.get("phase"), decision, reason,
         db.dumps({k: sig.get(k) for k in ("version", "revision", "reason", "setup", "side", "quote_time",
                                          "entry_zone", "target", "invalidation", "expires_at", "missing")})),
    )


def _decision(conn, order, t, status, reason, sig=None):
    conn.execute("UPDATE training_orders SET status=?,reason=?,updated_at=? WHERE id=? AND status='pending'",
                 (status, reason, _iso(t), order["id"]))
    original = {"opportunity_id": order["opportunity_id"], "phase": "confirmed", "quote_time": order["signal_quote_time"]}
    if sig and sig.get("opportunity_id") == order["opportunity_id"]:
        original.update(sig)
    _observe(conn, order["run_id"], order["variant"], order["code"], t, original, status, reason, order_id=order["id"])


def _expire(conn, run_id, t):
    for o in conn.execute("SELECT * FROM training_orders WHERE run_id=? AND status='pending' AND expires_at<=?",
                          (run_id, _iso(t))).fetchall():
        _decision(conn, o, t, "unfilled", "expired")


def _open(conn, run, cfg, quotes, t):
    baseline = {"cash_fen": cfg["cash_fen"], "allocations": {
        code: {"qty": qty, "price_fen": _fen(quotes[code]["price"]), "quote_time": quotes[code]["stamp"],
               "source": quotes[code]["source"]} for code, qty in cfg["quantities"].items()
    }}
    baseline["equity_fen"] = baseline["cash_fen"] + sum(
        p["qty"] * p["price_fen"] for p in baseline["allocations"].values()
    )
    holdings = {code: {"qty": qty, "locked": 0, "lock_day": None} for code, qty in cfg["quantities"].items()}
    conn.execute("UPDATE training_runs SET status='active',opened_at=?,baseline=? WHERE id=?",
                 (_iso(t), db.dumps(baseline), run["id"]))
    conn.execute("UPDATE training_books SET holdings=? WHERE run_id=?", (db.dumps(holdings), run["id"]))
    return baseline


def _price(side, px, slip):
    return _fen(px * (1 + slip if side == "buy" else 1 - slip))


def _fee(side, price_fen, qty):
    return _fen(sim.fees(side, _yuan(price_fen) * qty))


def _inside(price, zone):
    return zone[0] <= price <= zone[1]


def _crossed(side, price, rules):
    direction = 1 if side == "buy" else -1
    if direction * (price - rules["invalidation"]) <= 0:
        return "invalidation_crossed"
    if direction * (price - rules["target"]) >= 0:
        return "target_already_crossed"
    return None


def _signal_rules(sig, quote, t, end):
    raw_zone = sig.get("entry_zone")
    side = sig.get("side")
    expires = _stamp(sig.get("expires_at"))
    target = _number(sig.get("target"), positive=True)
    invalidation = _number(sig.get("invalidation"), positive=True)
    if side not in ("buy", "sell") or not expires or expires <= t or not isinstance(raw_zone, (list, tuple)) or len(raw_zone) != 2:
        return None
    low, high = (_number(x, positive=True) for x in raw_zone)
    if not low or not high or low > high or not target or not invalidation:
        return None
    if side == "sell" and not target < low <= high < invalidation:
        return None
    if side == "buy" and not invalidation < low <= high < target:
        return None
    return {"zone": [low, high], "target": target, "invalidation": invalidation,
            "expires": _iso(min(expires, end)), "source": quote["source"]}


def _available(pos, t):
    return pos["qty"] - (pos["locked"] if pos["lock_day"] == t.date().isoformat() else 0)


def _edge(qty, sale_fen, buy_fen):
    if sale_fen <= 0 or buy_fen <= 0:
        return -math.inf
    return (sale_fen - buy_fen) * qty - _fee("sell", sale_fen, qty) - _fee("buy", buy_fen, qty)


def _submit(conn, run, book, positions, code, sig, quote, cycle, t, end, slip):
    variant = book["variant"]
    side = sig["side"]
    op = sig.get("opportunity_id")

    def blocked(reason):
        _observe(conn, run["id"], variant, code, t, sig, "blocked", reason)

    if not isinstance(op, str) or not op:
        return blocked("missing_data")
    if conn.execute("SELECT 1 FROM training_orders WHERE run_id=? AND variant=? AND code=? AND opportunity_id=?",
                    (run["id"], variant, code, op)).fetchone():
        return  # A later revision cannot create a second order for the same opportunity.
    rules = _signal_rules(sig, quote, t, end)
    if not rules:
        return blocked("invalid_signal_terms")
    px = quote["price"]
    if not _inside(px, rules["zone"]):
        return blocked("outside_zone")
    if cycle:
        if side != "buy":
            return blocked("open_cycle_priority")
        qty = cycle["qty"]
        buy_fen = _price(side, px, slip)
        if book["cash"] < buy_fen * qty + _fee(side, buy_fen, qty):
            return blocked("insufficient_cash")
    else:
        if side != "sell":
            return blocked("no_open_cycle")
        last = conn.execute("SELECT MAX(closed_at) FROM training_cycles WHERE run_id=? AND variant=? AND code=?",
                            (run["id"], variant, code)).fetchone()[0]
        if last and t - _stamp(last) < COOLDOWN:
            return blocked("cooldown")
        avail = _available(positions[code], t)
        if avail < 100:
            return blocked("insufficient_sellable")
        qty = min(avail // 100 * 100, max(100, avail // 300 * 100))
        sale_fen = _price("sell", px, slip)
        if _edge(qty, sale_fen, _price("buy", rules["target"], slip)) * 10000 < sale_fen * qty * VARIANTS[variant]:
            return blocked("insufficient_net_edge")
    if book["mode"] == "shadow":
        _observe(conn, run["id"], variant, code, t, sig, "shadow", "paper_not_armed")
        return
    cur = conn.execute(
        "INSERT INTO training_orders(run_id,variant,code,side,qty,cycle_id,opportunity_id,submitted_at,"
        "signal_quote_time,expires_at,source,rules,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'pending')",
        (run["id"], variant, code, side, qty, cycle["id"] if cycle else None, op, _iso(t), quote["stamp"],
         rules["expires"], rules["source"], db.dumps(rules)),
    )
    _observe(conn, run["id"], variant, code, t, sig, "submitted", "wait_next_quote", order_id=cur.lastrowid)


def _pending(conn, order, book, positions, quote, sig, issue, cycle, t, slip):
    side, qty, code = order["side"], order["qty"], order["code"]
    if issue in ("market_defense", "risk_exit", "signal_invalid"):
        return _decision(conn, order, t, "invalid", issue, sig)
    if issue or not quote:
        _observe(conn, order["run_id"], order["variant"], code, t, sig, "blocked",
                 issue or "missing_data", order_id=order["id"])
        return
    if (quote["source"] != order["source"] or sig.get("opportunity_id") != order["opportunity_id"]
            or sig.get("side") != side):
        return _decision(conn, order, t, "invalid", "opportunity_or_source_changed", sig)
    if quote["stamp"] <= max(order["signal_quote_time"], order["submitted_at"]):
        return
    rules = db.loads(order["rules"])
    crossed = _crossed(side, quote["price"], rules)
    if crossed:
        return _decision(conn, order, t, "invalid", crossed, sig)
    if sig.get("phase") != "confirmed":
        return
    current_rules = _signal_rules(sig, quote, t, _session(t))
    if not current_rules:
        return _decision(conn, order, t, "invalid", "invalid_signal_terms", sig)
    price_fen = _price(side, quote["price"], slip)
    price = _yuan(price_fen)
    if not all(_inside(p, limits["zone"]) for p in (quote["price"], price) for limits in (rules, current_rules)):
        _observe(conn, order["run_id"], order["variant"], code, t, sig, "blocked",
                 "outside_zone", order_id=order["id"])
        return
    up = _number(quote.get("up_limit"), positive=True)
    dn = _number(quote.get("dn_limit"), positive=True)
    limit_reason = None
    if not up or not dn or dn >= up:
        limit_reason = "limit_data_missing"
    elif (side == "buy" and quote["price"] >= up - 0.001) or (side == "sell" and quote["price"] <= dn + 0.001):
        limit_reason = "limit_locked"
    elif not dn <= quote["price"] <= up or not dn <= price <= up:
        limit_reason = "price_outside_limits"
    if limit_reason:
        _observe(conn, order["run_id"], order["variant"], code, t, sig, "blocked",
                 limit_reason, order_id=order["id"])
        return
    pos = positions[code]
    fee_fen = _fee(side, price_fen, qty)
    amount = price_fen * qty
    if side == "sell":
        if cycle or qty > _available(pos, t):
            return _decision(conn, order, t, "unfilled", "insufficient_sellable_or_open_cycle", sig)
        if _edge(qty, price_fen, _price("buy", rules["target"], slip)) * 10000 < amount * VARIANTS[book["variant"]]:
            return _decision(conn, order, t, "unfilled", "insufficient_net_edge", sig)
        proceeds = amount - fee_fen
        pos["qty"] -= qty
        book["cash"] += proceeds
        cur = conn.execute(
            "INSERT INTO training_cycles(run_id,variant,code,qty,status,opened_at,sold_fen) VALUES(?,?,?,?,'open',?,?)",
            (order["run_id"], order["variant"], code, qty, _iso(t), proceeds),
        )
        cycle_id = cur.lastrowid
    else:
        if not cycle or cycle["id"] != order["cycle_id"] or qty != cycle["qty"]:
            return _decision(conn, order, t, "unfilled", "open_cycle_changed", sig)
        cost = amount + fee_fen
        if cost > book["cash"]:
            return _decision(conn, order, t, "unfilled", "insufficient_cash", sig)
        day = t.date().isoformat()
        pos["locked"] = (pos["locked"] if pos["lock_day"] == day else 0) + qty
        pos["lock_day"] = day
        pos["qty"] += qty
        book["cash"] -= cost
        cycle_id = cycle["id"]
        conn.execute("UPDATE training_cycles SET status='closed',closed_at=?,net_fen=? WHERE id=?",
                     (_iso(t), cycle["sold_fen"] - cost, cycle_id))
    conn.execute(
        "UPDATE training_orders SET cycle_id=?, status='filled',reason='next_quote',updated_at=?,"
        "fill_quote_time=?,fill_price_fen=?,fee_fen=? WHERE id=?",
        (cycle_id, _iso(t), quote["stamp"], price_fen, fee_fen, order["id"]),
    )
    _observe(conn, order["run_id"], order["variant"], code, t, sig, "filled", "next_quote", order_id=order["id"])


def _book_tick(conn, run, book, marks, quote_issues, signals, t, end, slip):
    positions = db.loads(book["holdings"])
    for code in positions:
        variant = book["variant"]
        by_variant = signals.get(code)
        sig = by_variant.get(variant) if isinstance(by_variant, dict) else None
        sig = sig if isinstance(sig, dict) else {}
        quote = marks.get(code)
        issue = _signal_issue(sig, quote, t, code, variant)
        if issue not in ("market_defense", "risk_exit", "signal_invalid"):
            issue = quote_issues.get(code) or issue
        pending = _row(conn, "SELECT * FROM training_orders WHERE run_id=? AND variant=? AND code=? AND status='pending'",
                       (run["id"], variant, code))
        cycle = _row(conn, "SELECT * FROM training_cycles WHERE run_id=? AND variant=? AND code=? AND status='open'",
                     (run["id"], variant, code))
        if pending:
            _pending(conn, pending, book, positions, quote, sig, issue, cycle, t, slip)
        elif issue:
            _observe(conn, run["id"], variant, code, t, sig, "invalid" if issue == "signal_invalid" else "blocked", issue)
        elif sig.get("phase") != "confirmed":
            _observe(conn, run["id"], variant, code, t, sig, "observed", sig.get("reason") or "not_confirmed")
        else:
            _submit(conn, run, book, positions, code, sig, quote, cycle, t, end, slip)
    conn.execute("UPDATE training_books SET cash=?,holdings=? WHERE run_id=? AND variant=?",
                 (book["cash"], db.dumps(positions), run["id"], book["variant"]))
    return positions


def _mark(conn, run_id, book, positions, baseline, quotes, t, slip):
    evidence = {code: {"price_fen": _fen(q["price"]), "quote_time": q["stamp"], "source": q["source"]}
                for code, q in quotes.items()}
    hold = baseline["cash_fen"] + sum(p["qty"] * evidence[code]["price_fen"]
                                      for code, p in baseline["allocations"].items())
    equity = book["cash"] + sum(p["qty"] * evidence[code]["price_fen"] for code, p in positions.items())
    restore = open_net = 0
    for cycle in conn.execute("SELECT * FROM training_cycles WHERE run_id=? AND variant=? AND status='open'",
                              (run_id, book["variant"])):
        qty = cycle["qty"]
        buy_price = _price("buy", quotes[cycle["code"]]["price"], slip)
        cost = buy_price * qty + _fee("buy", buy_price, qty)
        restore += cost
        open_net += cycle["sold_fen"] - cost
    conn.execute("INSERT OR REPLACE INTO training_marks VALUES(?,?,?,?,?,?,?,?,?)",
                 (run_id, book["variant"], t.date().isoformat(), _iso(t), equity, hold, restore, open_net, db.dumps(evidence)))


def tick(signals_by_code=None, quotes=None, asof=None) -> dict:
    """Consume one common observation. With no supplied quotes there is no network or execution."""
    t = _asof(asof)
    if not _exists():
        return {"version": VERSION, "status": "unconfigured"}
    signals = signals_by_code or {}
    end = _session(t)
    with db.tx() as conn:
        conn.execute("BEGIN IMMEDIATE")
        run = _active(conn)
        if not run:
            return {"version": VERSION, "status": "unconfigured"}
        summary = {"version": VERSION, "run_id": run["id"], "status": run["status"], "asof": _iso(t)}
        if _iso(t) < run["created_at"] or run["last_tick"] and _iso(t) <= run["last_tick"]:
            return {**summary, "reason": "duplicate_or_out_of_order"}
        conn.execute("UPDATE training_runs SET last_tick=? WHERE id=?", (_iso(t), run["id"]))
        _expire(conn, run["id"], t)
        if not end:
            return {**summary, "reason": "out_of_continuous_session"}

        cfg = db.loads(run["cfg"])
        books = [dict(x) for x in conn.execute("SELECT * FROM training_books WHERE run_id=?", (run["id"],))]
        marks, issues = {}, {}
        for code in cfg["quantities"]:
            mark, issue = _quote((quotes or {}).get(code), t, end)
            if mark:
                marks[code] = mark
            else:
                issues[code] = issue
        opening = run["status"] == "awaiting_session"
        if opening and issues:
            for book in books:
                for code, issue in issues.items():
                    _observe(conn, run["id"], book["variant"], code, t, None, "blocked", issue)
            return {**summary, "reason": "opening_quotes_incomplete", "blocked": dict(Counter(issues.values()))}
        slip = _number(load_settings().get("slippage"))
        if slip is None or not 0 <= slip < 1:
            return {**summary, "reason": "invalid_slippage_configuration"}
        baseline = _open(conn, run, cfg, marks, t) if opening else db.loads(run["baseline"])
        if opening:
            # Allocation is a baseline event, not a fill, and creates no first-tick orders.
            for book in conn.execute("SELECT * FROM training_books WHERE run_id=?", (run["id"],)):
                _mark(conn, run["id"], book, db.loads(book["holdings"]), baseline, marks, t, slip)
                for code in marks:
                    by_variant = signals.get(code)
                    sig = by_variant.get(book["variant"]) if isinstance(by_variant, dict) else None
                    if isinstance(sig, dict) and sig.get("opportunity_id"):
                        _observe(conn, run["id"], book["variant"], code, t, sig, "observed", "opening_allocation")
            return {**summary, "status": "active", "reason": "opening_allocation_frozen"}
        for book in books:
            positions = _book_tick(conn, run, book, marks, issues, signals, t, end, slip)
            if not issues:
                _mark(conn, run["id"], book, positions, baseline, marks, t, slip)
        return {**summary, "reason": "observed", "blocked": dict(Counter(issues.values()))}


def _experiment(conn, run, book):
    args = (run["id"], book["variant"])
    decisions = Counter()
    blockers = Counter()
    for o in conn.execute("SELECT decision,reason,COUNT(*) AS n FROM training_observations WHERE run_id=? AND variant=? "
                          "GROUP BY decision,reason", args):
        decisions[o["decision"]] += o["n"]
        if o["decision"] in ("blocked", "invalid", "unfilled"):
            blockers[o["reason"]] += o["n"]
    opportunities = conn.execute(
        "SELECT COUNT(DISTINCT opportunity_id), COUNT(DISTINCT CASE WHEN phase='confirmed' THEN opportunity_id END) "
        "FROM training_observations WHERE run_id=? AND variant=?", args,
    ).fetchone()
    orders = {r["status"]: r["n"] for r in conn.execute(
        "SELECT status,COUNT(*) AS n FROM training_orders WHERE run_id=? AND variant=? GROUP BY status", args
    )}
    completed, opened, profit = conn.execute(
        "SELECT COUNT(CASE WHEN status='closed' THEN 1 END), COUNT(CASE WHEN status='open' THEN 1 END),"
        "SUM(net_fen) FROM training_cycles WHERE run_id=? AND variant=?", args,
    ).fetchone()
    recent_marks = [dict(r) for r in conn.execute(
        "SELECT * FROM training_marks WHERE run_id=? AND variant=? ORDER BY date DESC LIMIT 120", args,
    )]
    baseline = db.loads(run["baseline"], {})
    initial = baseline.get("equity_fen")
    latest = recent_marks[0] if recent_marks else None
    equity = latest["equity_fen"] if latest else None
    hold = latest["hold_fen"] if latest else None
    current_mark = latest if latest and latest["marked_at"] == run["last_tick"] else None

    def mark_view(row):
        return {"date": row["date"], "asof": row["marked_at"], "equity": _yuan(row["equity_fen"]),
                "hold_equity": _yuan(row["hold_fen"]), "excess": _yuan(row["equity_fen"] - row["hold_fen"])}
    return {
        "mode": book["mode"], "cash": _yuan(book["cash"]), "opening_equity": _yuan(initial),
        "valuation": {"status": "recorded" if latest else "missing", "asof": latest["marked_at"] if latest else None,
                      "matches_last_tick": bool(current_mark),
                      "equity": _yuan(equity), "hold_equity": _yuan(hold),
                      "return_pct": round((equity / initial - 1) * 100, 4) if equity is not None and initial else None,
                      "hold_return_pct": round((hold / initial - 1) * 100, 4) if hold is not None and initial else None,
                      "excess": _yuan(equity - hold) if latest else None},
        "curve": [mark_view(row) for row in reversed(recent_marks)],
        "opportunities": {"seen": opportunities[0], "confirmed": opportunities[1], "decisions": dict(decisions),
                          "blocked_reasons": dict(blockers)},
        "orders": orders,
        "closed_cycles": {"count": completed, "net_after_fees": _yuan(profit),
                          "completion_rate": round(completed / (completed + opened), 4) if completed + opened else None},
        "open_risk": {"cycles": opened, "valuation_asof": current_mark["marked_at"] if current_mark else None,
                      "restore_cost_at_mark": _yuan(current_mark["restore_cash_fen"]) if current_mark else None,
                      "net_if_restored_at_mark": _yuan(current_mark["open_net_fen"]) if current_mark else None},
    }


def view() -> dict:
    result = {"version": VERSION, "status": "unconfigured", "experiments": {}}
    if not _exists():
        return result
    conn = db.conn()
    conn.execute("SAVEPOINT training_view")
    try:
        run = _active(conn)
        if not run:
            return result
        cfg = db.loads(run["cfg"])
        books = conn.execute("SELECT * FROM training_books WHERE run_id=? ORDER BY variant", (run["id"],)).fetchall()
        return {**result, "run_id": run["id"], "status": run["status"], "created_at": run["created_at"],
                "opened_at": run["opened_at"], "last_tick": run["last_tick"],
                "opening_inventory_count": len(cfg["quantities"]), "ignored_opening_buys": cfg["ignored_opening_buys"],
                "policy": {"comparison": "frozen_initial_inventory_and_cash", "mark": "last_complete_synchronized_quote",
                           "fill": "next_fresh_quote_with_slippage_and_fees", "corporate_actions": "not_adjusted",
                           "market_depth_verified": False, "automatic_end_of_day_trade": False},
                "experiments": {b["variant"]: _experiment(conn, run, b) for b in books}}
    finally:
        conn.execute("RELEASE training_view")

