"""Append-only forecasts and isolated cash-only paper books.

No calls to the legacy simulation execution API and no network operations. All
books share one immutable opening snapshot. Money is represented in integer fen.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, time, timedelta

from .. import db
from ..config import CN_TZ, now
from .calendar import HORIZONS, calendar_window, in_session, iso, normalize_horizon, official_days, stamp

BOOKS = ("chan_only", "math_only", "fusion", "legacy_control")
MAX_QUOTE_AGE = 12
FEES = {"commission": .00025, "min_commission": 5., "stamp_tax": .0005, "transfer": .00001}
SCHEMA = """
CREATE TABLE IF NOT EXISTS forecast_predictions(
 id TEXT PRIMARY KEY, code TEXT NOT NULL, horizon TEXT NOT NULL, asof TEXT NOT NULL,
 due_at TEXT NOT NULL, variant TEXT NOT NULL, model_version TEXT, origin TEXT NOT NULL,
 body TEXT NOT NULL, hash TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_forecast_due ON forecast_predictions(due_at);
CREATE TABLE IF NOT EXISTS forecast_results(
 prediction_id TEXT PRIMARY KEY, observed_at TEXT NOT NULL, status TEXT NOT NULL,
 outcome_return REAL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS forecast_books(
 id TEXT PRIMARY KEY, cash_fen INTEGER NOT NULL, holdings TEXT NOT NULL, baseline TEXT NOT NULL,
 opened_at TEXT NOT NULL, fees TEXT NOT NULL, slippage REAL NOT NULL, frozen_key TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS forecast_orders(
 id TEXT PRIMARY KEY, book TEXT NOT NULL, prediction_id TEXT NOT NULL, code TEXT NOT NULL,
 side TEXT NOT NULL, qty INTEGER NOT NULL, submitted_at TEXT NOT NULL,
 quote_time TEXT, source TEXT, expires_at TEXT, status TEXT NOT NULL, reason TEXT,
 reserved_fen INTEGER NOT NULL DEFAULT 0, body TEXT NOT NULL,
 fill_time TEXT, fill_price_fen INTEGER, fee_fen INTEGER);
CREATE INDEX IF NOT EXISTS ix_forecast_pending ON forecast_orders(book,status);
CREATE TABLE IF NOT EXISTS forecast_fills(
 order_id TEXT PRIMARY KEY, book TEXT NOT NULL, code TEXT NOT NULL, side TEXT NOT NULL,
 qty INTEGER NOT NULL, time TEXT NOT NULL, price_fen INTEGER NOT NULL,
 fee_fen INTEGER NOT NULL, slippage_fen INTEGER NOT NULL, model_version TEXT, variant TEXT,
 stress_extra_fen INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS forecast_cycles(
 id INTEGER PRIMARY KEY AUTOINCREMENT, book TEXT NOT NULL, code TEXT NOT NULL,
 side TEXT NOT NULL, qty INTEGER NOT NULL, remaining INTEGER NOT NULL, opened_at TEXT NOT NULL,
 price_fen INTEGER NOT NULL, fee_fen INTEGER NOT NULL, slippage_fen INTEGER NOT NULL,
 model_version TEXT);
CREATE TABLE IF NOT EXISTS forecast_closed_trades(
 id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id INTEGER NOT NULL, close_order_id TEXT NOT NULL,
 book TEXT NOT NULL, code TEXT NOT NULL, qty INTEGER NOT NULL, opened_at TEXT NOT NULL,
 closed_at TEXT NOT NULL, net_fen INTEGER NOT NULL, costs_fen INTEGER NOT NULL,
 model_version TEXT, UNIQUE(cycle_id,close_order_id));
CREATE TABLE IF NOT EXISTS forecast_marks(
 book TEXT NOT NULL, date TEXT NOT NULL, observed_at TEXT NOT NULL,
 equity_fen INTEGER NOT NULL, hold_fen INTEGER NOT NULL, costs_fen INTEGER NOT NULL,
 stress_equity_fen INTEGER NOT NULL, complete INTEGER NOT NULL, body TEXT NOT NULL,
 PRIMARY KEY(book,date));
CREATE TABLE IF NOT EXISTS forecast_ledger_events(
 id TEXT PRIMARY KEY, time TEXT NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL);
"""


def init():
    with db.tx() as conn:
        conn.executescript(SCHEMA)
        if 'stress_extra_fen' not in {r[1] for r in conn.execute('PRAGMA table_info(forecast_fills)')}:
            conn.execute('ALTER TABLE forecast_fills ADD COLUMN stress_extra_fen INTEGER NOT NULL DEFAULT 0')


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _num(value, *, positive=False):
    return (isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
            and (not positive or value > 0))


def _time(value=None):
    t = stamp(now() if value is None else value)
    if t is None:
        raise ValueError("invalid asof")
    return t


def _quote(raw, t, *, require_limits=False, closing=False):
    if not isinstance(raw, dict):
        return None, "missing_quote"
    px = raw.get("price")
    qt = stamp(raw.get("source_time") or raw.get("time"))
    received = stamp(raw.get("received_at")) if raw.get("received_at") else None
    if not _num(px, positive=True) or qt is None or not raw.get("source"):
        return None, "invalid_quote"
    if qt > t or (received and received > t):
        return None, "future_quote"
    age = (t - qt).total_seconds()
    if closing:
        if qt.date() != t.date() or qt.time().replace(tzinfo=None) != time(15) or t.time() < time(15):
            return None, "missing_close_quote"
    elif age > MAX_QUOTE_AGE:
        return None, "stale_quote"
    if raw.get("suspended") or raw.get("tradable") is False:
        return None, "suspended"
    if raw.get("trade_eligible") is False:
        return None, "ineligible_quote"
    if require_limits:
        upper = raw.get("limit_up", raw.get("up_limit"))
        lower = raw.get("limit_down", raw.get("dn_limit"))
        if not _num(upper, positive=True) or not _num(lower, positive=True) or lower >= upper:
            return None, "missing_price_limits"
        if not lower <= px <= upper:
            return None, "quote_outside_limits"
    else:
        upper, lower = None, None
    return {"price": px, "time": iso(qt), "source": str(raw["source"]), "upper": upper, "lower": lower}, None


def _fees(side, price_fen, qty, rates):
    amount = price_fen * qty
    return round(max(amount * rates["commission"], rates["min_commission"] * 100)
                 + amount * rates["transfer"] + (amount * rates["stamp_tax"] if side == "sell" else 0))


def _price(side, raw, slippage):
    return max(1, round(raw * 100 * (1 + slippage if side == "buy" else 1 - slippage)))


def _event(conn, kind, identity, t, body):
    conn.execute("INSERT OR IGNORE INTO forecast_ledger_events VALUES(?,?,?,?)",
                 (_hash([kind, identity]), iso(t), kind, _json(body)))


def book_identity(book):
    """Return canonical variant and optional immutable model epoch."""
    if book in BOOKS:
        return book, None
    match = re.fullmatch(r'(chan_only|math_only|fusion|legacy_control)@([1-9][0-9]*)', str(book))
    if not match:
        raise ValueError('invalid paper book identity')
    return match[1], int(match[2])


def record_prediction(record):
    """Idempotent append. The same identity with different contents is an error."""
    item = dict(record)
    item['horizon'] = normalize_horizon(item.get('horizon'))
    for field in ("code", "horizon", "asof", "due_at", "variant", "origin"):
        if not item.get(field):
            raise ValueError("missing prediction field: " + field)
    if not re.fullmatch(r"(?:sh|sz)\d{6}", item["code"]) or item["horizon"] not in HORIZONS:
        raise ValueError("unsupported prediction code/horizon")
    start, end = _time(item["asof"]), _time(item["due_at"])
    if end <= start or not _num(item.get("reference_price"), positive=True):
        raise ValueError("invalid prediction origin/deadline")
    source = stamp(item.get("source_time"))
    if source is None or source > start:
        raise ValueError("prediction requires past source timestamp")
    if item["variant"] not in BOOKS or item["origin"] not in ("forward", "historical", "synthetic"):
        raise ValueError("invalid prediction provenance")
    p = item.get("probability")
    if p is not None and (not _num(p) or not 0 <= p <= 1):
        raise ValueError("invalid probability")
    quantiles = item.get("quantiles") or {}
    values = [quantiles.get(k) for k in ("p10", "p50", "p90")]
    if any(v is not None for v in values) and (not all(_num(v) for v in values) or values != sorted(values)):
        raise ValueError("invalid quantiles")
    item.update(asof=iso(start), due_at=iso(end), source_time=iso(source))
    identity = item.get("id") or _hash([item[k] for k in ("code", "horizon", "asof", "variant")]
                                      + [item.get("model_version"), item.get("structure_id"), item["origin"]])
    item["id"] = identity
    raw, digest = _json(item), _hash(item)
    with db.tx() as conn:
        existing = conn.execute("SELECT hash FROM forecast_predictions WHERE id=?", (identity,)).fetchone()
        if existing:
            if existing["hash"] != digest:
                raise ValueError("immutable prediction conflict")
            return {"id": identity, "existing": True}
        conn.execute("INSERT INTO forecast_predictions VALUES(?,?,?,?,?,?,?,?,?,?)",
                     (identity, item["code"], item["horizon"], item["asof"], item["due_at"], item["variant"],
                      str(item.get("model_version") or "unvalidated"), item["origin"], raw, digest))
    return {"id": identity, "existing": False}


def settle(quotes, asof=None):
    """Settle only observed deadline evidence. Missing windows become immutable unknowns."""
    t = _time(asof)
    counts = {"settled": 0, "unknown": 0, "waiting": 0}
    with db.tx() as conn:
        # Coarse indexed second bucket, followed by a full-precision check below.
        # Existing whole-second ISO rows and new fractional rows remain compatible.
        upper = t.replace(microsecond=0) + timedelta(seconds=1)
        pending = conn.execute("SELECT p.* FROM forecast_predictions p LEFT JOIN forecast_results r "
                               "ON p.id=r.prediction_id WHERE r.prediction_id IS NULL AND p.due_at<?", (iso(upper),)).fetchall()
        for row in pending:
            due = _time(row["due_at"])
            if due > t:
                counts['waiting'] += 1
                continue
            raw = quotes.get(row["code"])
            quote, reason = _quote(raw, t)
            valid = quote and due <= _time(quote["time"]) <= due + timedelta(seconds=MAX_QUOTE_AGE)
            if valid:
                item = json.loads(row["body"])
                result = quote["price"] / item["reference_price"] - 1
                conn.execute("INSERT OR IGNORE INTO forecast_results VALUES(?,?,?,?,?)",
                             (row["id"], iso(t), "settled", result,
                              _json({"quote": quote, "due_at": row["due_at"], "latency_seconds": (t-due).total_seconds()})))
                counts["settled"] += 1
            elif t > due + timedelta(seconds=MAX_QUOTE_AGE):
                conn.execute("INSERT OR IGNORE INTO forecast_results VALUES(?,?,?,?,?)",
                             (row["id"], iso(t), "unknown", None,
                              _json({"reason": reason or "deadline_quote_missing", "due_at": row["due_at"]})))
                counts["unknown"] += 1
            else:
                counts["waiting"] += 1
    return counts


def predictions(limit=100, code=None):
    from .archive import read_record
    where, args = ("WHERE p.code=?", [code]) if code else ("", [])
    rows = db.q("SELECT p.body,r.status,r.outcome_return,r.observed_at,r.body AS outcome_body "
                "FROM forecast_predictions p LEFT JOIN forecast_results r ON p.id=r.prediction_id "
                + where + " ORDER BY p.asof DESC,p.id LIMIT ?", (*args, max(1, min(int(limit), 5000))))
    return [{**read_record(r["body"]), "result": {"status": r["status"] or "pending", "outcome_return": r["outcome_return"],
             "observed_at": r["observed_at"], **json.loads(r["outcome_body"] or "{}")}} for r in rows]


def training_rows(limit=100000):
    from .archive import read_record
    # Freeze the first origin per stock/horizon/AM-PM before inspecting its outcome.
    # Otherwise thousands of near-identical short forecasts exhaust the bounded
    # training window before long-horizon labels can ever enter it. All original
    # opportunities remain in the evaluation ledger; missing first origins are
    # not replaced with a more favourable later observation.
    rows = db.q('''WITH origins AS (
      SELECT *,ROW_NUMBER() OVER(PARTITION BY code,horizon,substr(asof,1,10),
        CASE WHEN substr(asof,12,2)<'12' THEN 'AM' ELSE 'PM' END
        ORDER BY asof,id) AS first_origin FROM forecast_predictions WHERE variant='fusion')
      SELECT p.body,r.outcome_return,r.observed_at FROM origins p JOIN forecast_results r ON p.id=r.prediction_id
      WHERE p.first_origin=1 AND r.status='settled' ORDER BY p.asof DESC,p.id DESC LIMIT ?''',(min(int(limit),100000),))
    return [{**read_record(r["body"]), "outcome_return": r["outcome_return"], "outcome_observed_at": r["observed_at"]} for r in rows]


def initialize(cash, holdings, quotes, asof=None, fees=None, slippage=.001, *, _book_names=None, _parent=None):
    t = _time(asof)
    if not _num(cash) or cash < 0 or not _num(slippage) or not 0 <= slippage <= .1:
        raise ValueError("cash-only opening requires nonnegative cash and bounded slippage")
    rates = {**FEES, **(fees or {})}
    if any(not _num(v) or v < 0 for v in rates.values()):
        raise ValueError("invalid fee configuration")
    positions, prices = {}, {}
    for hold in holdings:
        code, qty = hold.get("code"), hold.get("qty")
        if not isinstance(code, str) or not re.fullmatch(r"(?:sh|sz)\d{6}", code):
            raise ValueError("invalid opening code")
        if code in positions or not _num(qty) or qty <= 0 or qty % 100:
            raise ValueError("opening holdings must be positive unique whole lots; shorts are not silently omitted")
        quote, reason = _quote(quotes.get(code), t)
        if not quote:
            raise ValueError("opening valuation unavailable: " + str(reason))
        raw_lots = hold.get('lots') or [{'qty':qty,'acquired_at':hold.get('acquired_at') or hold.get('last_buy') or t}]
        lots = []
        for lot in raw_lots:
            acquired = _time(lot['acquired_at'])
            if acquired > t or not _num(lot.get('qty')) or lot['qty']<=0 or lot['qty']%100:
                raise ValueError("invalid opening lot/acquisition")
            lots.append({'qty':int(lot['qty']),'acquired_at':iso(acquired)})
        if sum(lot['qty'] for lot in lots)!=qty:
            raise ValueError('opening lot quantity mismatch')
        positions[code] = {"qty": int(qty), "lots": lots}
        prices[code] = quote["price"]
    baseline = {"cash_fen": round(cash * 100), "quantities": {c: p["qty"] for c, p in positions.items()},
                "prices": prices, "equity_fen": round(cash * 100) + sum(round(prices[c]*100)*p["qty"] for c,p in positions.items()),
                "fees": rates, "slippage": slippage, "opened_at": iso(t)}
    names = tuple(_book_names or BOOKS)
    if len(names)!=4 or len({book_identity(name)[0] for name in names})!=4 or len({book_identity(name)[1] for name in names})!=1:
        raise ValueError('opening requires one complete epoch')
    if _parent:
        baseline.update(_parent)
    digest = _hash({'baseline':baseline,'books':names})
    with db.tx() as conn:
        existing = conn.execute("SELECT frozen_key FROM forecast_books WHERE id IN (?,?,?,?)",names).fetchall()
        if existing:
            if len(existing) != len(BOOKS) or any(x["frozen_key"] != digest for x in existing):
                raise ValueError("frozen opening already exists; cannot rebase")
            return {"existing": True, "frozen_key": digest}
        for book in names:
            conn.execute("INSERT INTO forecast_books VALUES(?,?,?,?,?,?,?,?)",
                         (book, round(cash*100), _json(positions), _json(baseline), iso(t), _json(rates), slippage, digest))
        _event(conn, "opening", digest, t, {"books": list(names), "frozen_key": digest})
    return {"existing": False, "frozen_key": digest, "books":list(names)}


def initialize_version(version, quotes, asof=None, source_book='legacy_control'):
    """Freeze four comparable books for a candidate without overwriting any ancestor."""
    if not isinstance(version,int) or isinstance(version,bool) or version<=0:
        raise ValueError('model epoch must be positive integer')
    names=tuple(f'{variant}@{version}' for variant in BOOKS)
    with db.tx() as conn:
        existing=conn.execute('SELECT frozen_key FROM forecast_books WHERE id IN (?,?,?,?)',names).fetchall()
        if existing:
            if len(existing)!=4 or len({r['frozen_key'] for r in existing})!=1:
                raise ValueError('partial epoch opening')
            return {'existing':True,'frozen_key':existing[0]['frozen_key'],'books':list(names)}
        source=_book(conn,source_book)
        baseline=json.loads(source['baseline'])
        holdings=[{'code':code,**position} for code,position in json.loads(source['holdings']).items()]
        parent={'source_book':source_book,'parent_frozen_key':source['frozen_key'],
                'root_baseline':baseline.get('root_baseline',baseline),'model_version':version}
        # Same write lock covers the source snapshot and the atomic four-book insertion.
        return initialize(source['cash_fen']/100,holdings,quotes,asof,
                          fees=json.loads(source['fees']),slippage=source['slippage'],_book_names=names,_parent=parent)


def _available(position, t):
    return sum(lot["qty"] for lot in position.get("lots", []) if _time(lot["acquired_at"]).date() < t.date())


def _book(conn, name):
    row = conn.execute("SELECT * FROM forecast_books WHERE id=?", (name,)).fetchone()
    if not row:
        raise ValueError("unknown paper book")
    return dict(row)


def submit(book, code, side, qty, forecast_id, quote, asof=None, reason="", expires_at=None,
           *, entry_zone=None, intent="position"):
    t = _time(asof)
    variant, epoch = book_identity(book)
    if side not in ("buy", "sell"):
        raise ValueError("invalid book/side")
    if not isinstance(code, str) or not re.fullmatch(r"(?:sh|sz)\d{6}", code):
        raise ValueError("invalid code")
    if not _num(qty) or qty <= 0 or qty % 100:
        raise ValueError("quantity must be positive 100-share lots")
    if not isinstance(forecast_id, str) or not forecast_id:
        raise ValueError("stable prediction/signal identity required")
    qty = int(qty)
    identity = _hash([book, code, side, forecast_id])
    deadline = _time(expires_at) if expires_at else min(t+timedelta(minutes=5), t.replace(hour=15, minute=0, second=0, microsecond=0))
    raw, problem = _quote(quote, t, require_limits=True)
    if not in_session(t, official_days(t.year)):
        problem = "outside_session_or_unknown_calendar"
    if deadline <= t or deadline.date() != t.date():
        problem = "invalid_order_expiry"
    zone = entry_zone or ([raw["price"]*.995, raw["price"]*1.005] if raw else None)
    if zone is not None and (not isinstance(zone, (list, tuple)) or len(zone) != 2
                             or not all(_num(v, positive=True) for v in zone) or zone[0] > zone[1]):
        raise ValueError("invalid entry zone")
    with db.tx() as conn:
        existing = conn.execute("SELECT * FROM forecast_orders WHERE id=?", (identity,)).fetchone()
        if existing:
            if existing["qty"] != qty:
                raise ValueError("order identity conflict")
            return {"id": identity, "status": existing["status"], "reason": existing["reason"], "existing": True}
        account = _book(conn, book)
        prediction = conn.execute("SELECT * FROM forecast_predictions WHERE id=?", (forecast_id,)).fetchone()
        if prediction:
            body = json.loads(prediction["body"])
            if prediction["origin"] != "forward" or prediction["variant"] != variant or prediction["code"] != code:
                problem = "prediction_not_forward_or_wrong_book"
            elif epoch is not None and prediction['model_version']!=str(epoch):
                problem = 'prediction_wrong_epoch'
            elif _time(prediction["asof"]) > t or _time(prediction["due_at"]) <= t:
                problem = "prediction_not_current"
            elif not (body.get("quality") or {}).get("eligible", False):
                problem = "prediction_ineligible"
        elif variant != "legacy_control":
            problem = "missing_prediction"
        holdings = json.loads(account["holdings"])
        rates = json.loads(account["fees"])
        reserve = 0
        if raw and not problem:
            if side == "buy" and raw["price"] >= raw["upper"] or side == "sell" and raw["price"] <= raw["lower"]:
                problem = "price_limit_locked"
            elif side == "sell":
                pending = conn.execute("SELECT COALESCE(SUM(qty),0) FROM forecast_orders WHERE book=? AND code=? "
                                       "AND side='sell' AND status='pending'", (book, code)).fetchone()[0]
                if _available(holdings.get(code, {}), t) - pending < qty:
                    problem = "insufficient_sellable_t1"
            else:
                px = _price(side, min(zone[1], raw["upper"]), account["slippage"])
                reserve = px*qty + _fees(side, px, qty, rates)
                pending = conn.execute("SELECT COALESCE(SUM(reserved_fen),0) FROM forecast_orders WHERE book=? "
                                       "AND side='buy' AND status='pending'", (book,)).fetchone()[0]
                if account["cash_fen"] - pending < reserve:
                    problem = "insufficient_cash"
        status = "rejected" if problem else "pending"
        payload = {"entry_zone": zone, "intent": intent, "reason": reason,
                   "model_version": prediction["model_version"] if prediction else str(epoch) if epoch else "legacy", "origin": "forward"}
        conn.execute("INSERT INTO forecast_orders(id,book,prediction_id,code,side,qty,submitted_at,quote_time,source,"
                     "expires_at,status,reason,reserved_fen,body) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (identity,book,forecast_id,code,side,qty,iso(t),raw["time"] if raw else None,
                      raw["source"] if raw else None,iso(deadline),status,problem or reason,
                      reserve if not problem else 0,_json(payload)))
        return {"id": identity, "status": status, "reason": problem or reason, "existing": False}


def _match_cycles(conn, order, price, fee, slip, t, model_version):
    remaining = order["qty"]
    cycles = conn.execute("SELECT * FROM forecast_cycles WHERE book=? AND code=? AND side<>? AND remaining>0 "
                          "ORDER BY id", (order["book"],order["code"],order["side"])).fetchall()
    for cycle in cycles:
        qty = min(remaining, cycle["remaining"])
        if not qty:
            break
        # Allocate residual fen deterministically, so partial closes sum to the
        # exact original minimum commission instead of rounding it repeatedly.
        allocate = lambda total, before, take, full: round(total*before/full)-round(total*(before-take)/full)
        open_fee = allocate(cycle["fee_fen"],cycle['remaining'],qty,cycle['qty'])
        close_fee = allocate(fee,remaining,qty,order['qty'])
        costs = open_fee + close_fee + allocate(cycle['slippage_fen'],cycle['remaining'],qty,cycle['qty']) + allocate(slip,remaining,qty,order['qty'])
        gross = (price-cycle["price_fen"])*qty*(1 if cycle["side"] == "buy" else -1)
        conn.execute("INSERT INTO forecast_closed_trades(cycle_id,close_order_id,book,code,qty,opened_at,closed_at,"
                     "net_fen,costs_fen,model_version) VALUES(?,?,?,?,?,?,?,?,?,?)",
                     (cycle["id"],order["id"],order["book"],order["code"],qty,cycle["opened_at"],iso(t),
                      gross-open_fee-close_fee,costs,cycle["model_version"]))
        conn.execute("UPDATE forecast_cycles SET remaining=remaining-? WHERE id=?",(qty,cycle["id"]))
        remaining -= qty
    if remaining:
        conn.execute("INSERT INTO forecast_cycles(book,code,side,qty,remaining,opened_at,price_fen,fee_fen,slippage_fen,"
                     "model_version) VALUES(?,?,?,?,?,?,?,?,?,?)",
                     (order["book"],order["code"],order["side"],remaining,remaining,iso(t),price,
                      round(fee*remaining/order["qty"]),round(slip*remaining/order["qty"]),model_version))


def match(quotes, asof=None):
    t = _time(asof)
    result = {"filled": 0, "expired": 0, "blocked": 0, "rejected": 0}
    with db.tx() as conn:
        orders = conn.execute("SELECT * FROM forecast_orders WHERE status='pending' ORDER BY submitted_at,id").fetchall()
        for row in orders:
            order = dict(row)
            if t > _time(order["expires_at"]):
                conn.execute("UPDATE forecast_orders SET status='expired',reason='expired',reserved_fen=0 WHERE id=?",(order["id"],))
                result["expired"] += 1
                continue
            quote, issue = _quote(quotes.get(order["code"]), t, require_limits=True)
            if not in_session(t, official_days(t.year)):
                issue = "outside_session_or_unknown_calendar"
            if quote and (_time(quote["time"]) <= max(_time(order["quote_time"]),_time(order["submitted_at"])) or quote["source"] != order["source"]):
                issue = "wait_next_same_source_quote"
            body = json.loads(order["body"])
            if quote and not issue and not body["entry_zone"][0] <= quote["price"] <= body["entry_zone"][1]:
                issue = "outside_entry_zone"
            if quote and not issue and (order["side"] == "buy" and quote["price"] >= quote["upper"]
                                       or order["side"] == "sell" and quote["price"] <= quote["lower"]):
                issue = "price_limit_locked"
            if issue:
                _event(conn,"order_blocked",[order["id"],issue,iso(t)],t,{"order_id":order["id"],"reason":issue})
                result["blocked"] += 1
                continue
            account = _book(conn,order["book"])
            holdings = json.loads(account["holdings"])
            position = holdings.setdefault(order["code"],{"qty":0,"lots":[]})
            side, qty = order["side"], order["qty"]
            price = _price(side,quote["price"],account["slippage"])
            if price > round(quote["upper"]*100) or price < round(quote["lower"]*100):
                result["blocked"] += 1
                continue
            rates = json.loads(account["fees"])
            fee = _fees(side,price,qty,rates)
            slip = abs(price-round(quote["price"]*100))*qty
            stress_price = _price(side,quote['price'],account['slippage']*1.5)
            stress_fee = _fees(side,stress_price,qty,{k:v*1.5 for k,v in rates.items()})
            stress_extra = max(0,(stress_price-price)*qty*(1 if side=='buy' else -1)+stress_fee-fee)
            if side == "buy":
                other_reserved = conn.execute("SELECT COALESCE(SUM(reserved_fen),0) FROM forecast_orders "
                                              "WHERE book=? AND status='pending' AND side='buy' AND id<>?",(order["book"],order["id"])).fetchone()[0]
                if account["cash_fen"]-other_reserved < price*qty+fee:
                    issue = "insufficient_cash_at_fill"
                else:
                    account["cash_fen"] -= price*qty+fee
                    position["qty"] += qty
                    position["lots"].append({"qty":qty,"acquired_at":iso(t)})
            elif _available(position,t) < qty:
                issue = "insufficient_sellable_at_fill"
            else:
                left = qty
                for lot in position["lots"]:
                    if _time(lot["acquired_at"]).date() < t.date():
                        take = min(left,lot["qty"])
                        lot["qty"] -= take
                        left -= take
                position["lots"] = [lot for lot in position["lots"] if lot["qty"]]
                position["qty"] -= qty
                account["cash_fen"] += price*qty-fee
            if issue:
                conn.execute("UPDATE forecast_orders SET status='rejected',reason=?,reserved_fen=0 WHERE id=?",(issue,order["id"]))
                result["rejected"] += 1
                continue
            holdings = {code:p for code,p in holdings.items() if p["qty"]}
            conn.execute("UPDATE forecast_books SET cash_fen=?,holdings=? WHERE id=?",(account["cash_fen"],_json(holdings),order["book"]))
            conn.execute("UPDATE forecast_orders SET status='filled',fill_time=?,fill_price_fen=?,fee_fen=?,reserved_fen=0 WHERE id=?",
                         (iso(t),price,fee,order["id"]))
            prior_stress = conn.execute('SELECT COALESCE(SUM(stress_extra_fen),0) FROM forecast_fills WHERE book=?',
                                        (order['book'],)).fetchone()[0]
            conn.execute("INSERT INTO forecast_fills VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                         (order["id"],order["book"],order["code"],side,qty,iso(t),price,fee,slip,body["model_version"],order["book"],stress_extra))
            if account['cash_fen']-prior_stress-stress_extra < 0 or not round(quote['lower']*100) <= stress_price <= round(quote['upper']*100):
                _event(conn,'stress_infeasible',order['id'],t,{'book':order['book'],'model_version':body['model_version']})
            _match_cycles(conn,order,price,fee,slip,t,body["model_version"])
            result["filled"] += 1
    return result


def cancel_pending(reason, asof=None):
    t = _time(asof)
    with db.tx() as conn:
        ids = [r[0] for r in conn.execute("SELECT id FROM forecast_orders WHERE status='pending'")]
        conn.execute("UPDATE forecast_orders SET status='cancelled',reason=?,reserved_fen=0 WHERE status='pending'", (str(reason),))
        _event(conn,"cancel_pending",[iso(t),reason],t,{"reason":reason,"count":len(ids)})
    return len(ids)


def mark(quotes, asof=None, model_version=None):
    t = _time(asof)
    if t.date().isoformat() not in official_days(t.year):
        return {"complete":False,"reason":"unknown_calendar_or_closed_day"}
    if t.time() < time(15):
        return {"complete":False,"reason":"not_close","intraday_only":True}
    with db.tx() as conn:
        books = [dict(r) for r in conn.execute("SELECT * FROM forecast_books")]
        if not books or any(sum(book_identity(b['id'])[1]==epoch for b in books)!=4 for epoch in {book_identity(b['id'])[1] for b in books}):
            return {"complete":False,"reason":"unconfigured"}
        codes = set()
        for book in books:
            codes.update(json.loads(book["holdings"]))
            codes.update(json.loads(book["baseline"])["quantities"])
        valid, missing = {}, {}
        for code in codes:
            q, issue = _quote(quotes.get(code),t,closing=True)
            if issue: missing[code] = issue
            else: valid[code] = q
        if missing:
            _event(conn,"close_missing",t.date().isoformat(),t,{"date":t.date().isoformat(),"missing":missing})
            return {"complete":False,"reason":"close_quote_gap","missing":missing}
        values = {}
        for book in books:
            baseline, positions = json.loads(book["baseline"]), json.loads(book["holdings"])
            eq = book["cash_fen"]+sum(round(valid[c]["price"]*100)*p["qty"] for c,p in positions.items())
            hold = baseline["cash_fen"]+sum(round(valid[c]["price"]*100)*qty for c,qty in baseline["quantities"].items())
            cost = conn.execute("SELECT COALESCE(SUM(fee_fen+slippage_fen),0) FROM forecast_fills WHERE book=?",(book["id"],)).fetchone()[0]
            stress_extra = conn.execute('SELECT COALESCE(SUM(stress_extra_fen),0) FROM forecast_fills WHERE book=?',
                                        (book['id'],)).fetchone()[0]
            stress_ok = book["cash_fen"] - stress_extra >= 0
            evidence = {"origin":"forward","quote_time":t.date().isoformat()+"T15:00:00+08:00","stress_feasible":stress_ok,
                        "model_version":book_identity(book['id'])[1] or model_version,
                        "closed_trades":conn.execute("SELECT COUNT(*) FROM forecast_cycles WHERE book=? AND remaining=0",(book["id"],)).fetchone()[0]}
            conn.execute("INSERT OR IGNORE INTO forecast_marks VALUES(?,?,?,?,?,?,?,?,?)",
                         (book["id"],t.date().isoformat(),iso(t),eq,hold,cost,eq-stress_extra,1,_json(evidence)))
            values[book["id"]] = {"equity":eq/100,"hold_equity":hold/100,"stress_feasible":stress_ok}
        return {"complete":True,"date":t.date().isoformat(),"books":values}


def view():
    if not db.q1("SELECT 1 FROM sqlite_master WHERE name='forecast_books'"):
        return {"status":"unconfigured","books":{},"predictions":[]}
    books = db.q("SELECT * FROM forecast_books ORDER BY id")
    result = {}
    for b in books:
        variant,epoch = book_identity(b['id'])
        result[b["id"]] = {"variant":variant,"model_version":epoch,"epoch":epoch,
                           "cash":b["cash_fen"]/100,"holdings":json.loads(b["holdings"]),"baseline":json.loads(b["baseline"]),
                           "opened_at":b["opened_at"],"orders":db.q("SELECT * FROM forecast_orders WHERE book=? ORDER BY submitted_at DESC LIMIT 50",(b["id"],)),
                           "latest_mark":db.q1("SELECT * FROM forecast_marks WHERE book=? ORDER BY date DESC LIMIT 1",(b["id"],)),
                           "open_cycles":db.q("SELECT * FROM forecast_cycles WHERE book=? AND remaining>0",(b["id"],))}
    return {"status":"active" if len(books)>=len(BOOKS) else "unconfigured","books":result,"predictions":predictions(100)}


def evaluation_data(candidate="fusion", model_version=None):
    if candidate not in BOOKS or candidate == "legacy_control":
        raise ValueError("invalid evaluation candidate")
    books = {r["id"]:r for r in db.q("SELECT * FROM forecast_books")}
    candidate_book=f'{candidate}@{model_version}' if model_version is not None else candidate
    control_book=f'legacy_control@{model_version}' if model_version is not None else 'legacy_control'
    if candidate_book not in books or control_book not in books:
        return {"origin":"forward","complete":False,"predictions":[],"daily":[],"closed_trades":0,"missing":0,"failed":0,"reason":"unconfigured"}
    clauses, args = ["p.variant=?"], [candidate]
    if model_version is not None:
        clauses.append("p.model_version=?"); args.append(str(model_version))
    records = db.q("SELECT p.body,r.status,r.outcome_return FROM forecast_predictions p LEFT JOIN forecast_results r "
                   "ON p.id=r.prediction_id WHERE "+" AND ".join(clauses)+" ORDER BY p.asof",args)
    outputs, missing = [], 0
    for row in records:
        item = json.loads(row["body"])
        if row["status"] == "unknown": missing += 1
        outputs.append({**item,"status":row["status"] or "pending","outcome_return":row["outcome_return"]})
    marks = db.q("SELECT * FROM forecast_marks WHERE book IN (?,?) ORDER BY date,book",(candidate_book,control_book))
    paired = {}
    for row in marks: paired.setdefault(row["date"],{})[row["book"]] = row
    daily = []
    prev = {b:json.loads(books[b]["baseline"])["equity_fen"] for b in (candidate_book,control_book)}
    costs = {b:0 for b in prev}
    stress_prev = dict(prev)
    complete = bool(paired)
    for day, pair in sorted(paired.items()):
        if len(pair)!=2 or any(not row["complete"] for row in pair.values()):
            complete = False
            continue
        line = {"date":day,"origin":"forward"}
        for b,label in ((candidate_book,"candidate"),(control_book,"baseline")):
            if prev[b]<=0:
                complete = False
                break
            line[label+"_return"] = pair[b]["equity_fen"]/prev[b]-1
            line[label+"_cost"] = (pair[b]["costs_fen"]-costs[b])/prev[b]
            line[label+'_stress_return'] = pair[b]['stress_equity_fen']/stress_prev[b]-1 if stress_prev[b]>0 else None
            stress_prev[b] = pair[b]['stress_equity_fen']
            prev[b],costs[b] = pair[b]["equity_fen"],pair[b]["costs_fen"]
        epoch = json.loads(pair[candidate_book]['body']).get('model_version')
        line['model_version'] = epoch
        if "baseline_return" in line and (model_version is None or str(epoch) == str(model_version)):
            daily.append(line)
    start_day = max(books[candidate_book]['opened_at'][:10],min((p['asof'][:10] for p in outputs), default=books[candidate_book]['opened_at'][:10]))
    end_day = max((r['date'] for r in daily), default=start_day)
    expected_days = {d for d in calendar_window(int(start_day[:4])) if start_day <= d <= end_day}
    missing_mark_days = expected_days - {r['date'] for r in daily}
    failed = db.q1("SELECT COUNT(*) AS n FROM forecast_orders WHERE book=? AND status IN ('expired','rejected') "
                    "AND submitted_at>=? AND substr(submitted_at,1,10)<=?",(candidate_book,start_day,end_day))["n"]
    close_gaps = db.q1("SELECT COUNT(*) AS n FROM forecast_ledger_events WHERE kind='close_missing' "
                       "AND time>=? AND substr(time,1,10)<=?",(start_day,end_day))["n"]
    legacy_gaps = sum(json.loads(r['body']).get('book')==control_book for r in db.q(
        "SELECT body FROM forecast_ledger_events WHERE kind='legacy_data_gap' AND time>=? AND substr(time,1,10)<=?",
        (start_day,end_day)))
    closed = db.q1("SELECT COUNT(*) AS n FROM forecast_cycles WHERE book=? AND remaining=0"+
                   (" AND model_version=?" if model_version is not None else ""),
                   (candidate_book,str(model_version)) if model_version is not None else (candidate_book,))["n"]
    stress_events = [json.loads(r['body']) for r in db.q("SELECT body FROM forecast_ledger_events WHERE kind='stress_infeasible' "
                                                       "AND time>=? AND substr(time,1,10)<=?",(start_day,end_day))]
    stress_infeasible = any(e.get('book') in (candidate_book,control_book) and
                            (model_version is None or str(e.get('model_version')) in (str(model_version),'legacy')) for e in stress_events)
    return {"origin":"forward","model_version":model_version,
            "complete":complete and not missing and not missing_mark_days and not legacy_gaps,"predictions":outputs,"daily":daily,
            "closed_trades":closed,"missing":missing+len(missing_mark_days)+legacy_gaps,"failed":failed,"open_cycles":db.q1(
                "SELECT COUNT(*) AS n FROM forecast_cycles WHERE book=? AND remaining>0",(candidate_book,))["n"],
            "stress_infeasible":stress_infeasible,
            "missing_mark_dates":sorted(missing_mark_days),
            "close_gap_events":close_gaps,
            "legacy_gap_events":legacy_gaps,
            "scope":"frozen_opening_and_all_recorded_opportunities","cost_stress":"frozen_orders_1.5x_fees_and_slippage"}
