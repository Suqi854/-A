"""Versioned intraday evidence and shadow opportunities for the simulation engine."""
from __future__ import annotations

import hashlib
import json
import math
import threading
from datetime import datetime, time, timedelta
from statistics import median

from . import db
from .config import CN_TZ, MOCK, now
from .indicators import macd

VERSION = "intraday-1"
VARIANTS = {
    "fast": (30, 0.8, 8, 90),
    "balanced": (60, 1.0, 15, 180),
    "slow": (60, 1.2, 25, 300),
}
MAX_SAMPLE_GAP = 12
MAX_SAMPLES = 5200
ACTIVE = {"candidate", "watch", "confirmed"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS intraday_samples(
    code TEXT NOT NULL, quote_time TEXT NOT NULL, payload TEXT NOT NULL,
    PRIMARY KEY(code, quote_time));
CREATE INDEX IF NOT EXISTS ix_intraday_samples_time ON intraday_samples(quote_time);
CREATE TABLE IF NOT EXISTS intraday_latest(
    code TEXT NOT NULL, variant TEXT NOT NULL, payload TEXT NOT NULL,
    PRIMARY KEY(code, variant));
CREATE TABLE IF NOT EXISTS intraday_revisions(
    opportunity_id TEXT NOT NULL, revision INTEGER NOT NULL, code TEXT NOT NULL,
    variant TEXT NOT NULL, evaluated_at TEXT NOT NULL, payload TEXT NOT NULL,
    PRIMARY KEY(opportunity_id, revision));
CREATE TABLE IF NOT EXISTS intraday_coverage(
    code TEXT PRIMARY KEY, accepted INTEGER DEFAULT 0, duplicate INTEGER DEFAULT 0,
    late INTEGER DEFAULT 0, stale INTEGER DEFAULT 0, missing INTEGER DEFAULT 0,
    resets INTEGER DEFAULT 0, last_quote TEXT, last_eval TEXT, last_gap TEXT);
"""


def init() -> None:
    with db.tx() as conn:
        conn.executescript(SCHEMA)


def _stamp(value) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(CN_TZ) if value.tzinfo else value.replace(tzinfo=CN_TZ)
    if not isinstance(value, str) or not value:
        return None
    try:
        if value.isascii() and value.isdecimal() and len(value) in (12, 14):
            return datetime.strptime(value, "%Y%m%d%H%M%S" if len(value) == 14 else "%Y%m%d%H%M").replace(tzinfo=CN_TZ)
        return _stamp(datetime.fromisoformat(value))
    except ValueError:
        return None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


def _asof(value) -> datetime:
    result = now() if value is None else _stamp(value)
    if result is None:
        raise ValueError("asof must be a timestamp")
    return result


def _session(ts: datetime) -> tuple[datetime, datetime] | None:
    if ts.weekday() >= 5:
        return None
    for start, end in ((time(9, 30), time(11, 30)), (time(13), time(14, 57))):
        if start <= ts.time() < end:
            return (datetime.combine(ts.date(), start, CN_TZ), datetime.combine(ts.date(), end, CN_TZ))
    return None


def _number(value, *, positive=False) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        v = float(value)
        return v if math.isfinite(v) and (not positive or v > 0) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _sample(code, quote, received_at, source) -> dict | None:
    quote = quote or {}
    stamp = _stamp(quote.get("quote_time") or quote.get("time"))
    source = source or quote.get("source")
    price = _number(quote.get("price"), positive=True)
    if not stamp or not price or not isinstance(source, str) or not source.strip() or quote.get("suspended"):
        return None
    return {"code": code, "quote_time": _iso(stamp), "received_at": _iso(received_at), "source": source.strip(),
            "price": price, **{k: _number(quote.get(k), positive=k != "pct")
                              for k in ("vol", "amount", "prev", "pct")}}


def _counter_reset(newer, older):
    return any(newer[k] is not None and older[k] is not None and newer[k] < older[k]
               for k in ("vol", "amount"))


def _coverage(conn, code, field, stamp=None, gap=None):
    conn.execute("INSERT OR IGNORE INTO intraday_coverage(code) VALUES(?)", (code,))
    if field == "evaluation":
        conn.execute("UPDATE intraday_coverage SET last_eval=?, last_gap=? WHERE code=?", (stamp, gap, code))
    elif field == "accepted":
        conn.execute("UPDATE intraday_coverage SET accepted=accepted+1, last_quote=?, "
                     "resets=resets+?, last_gap=? WHERE code=?", (stamp, int(bool(gap)), gap, code))
    else:
        conn.execute(f"UPDATE intraday_coverage SET {field}={field}+1, last_gap=? WHERE code=?", (gap, code))


def _ingest(conn, code, quote, received_at, source) -> bool:
    sample = _sample(code, quote, received_at, source)
    if not sample:
        _coverage(conn, code, "missing", gap="quote_timestamp_price_or_source_missing")
        return False
    ts = _stamp(sample["quote_time"])
    session = _session(received_at)
    if not session or _session(ts) != session or ts > received_at or (received_at - ts).total_seconds() > MAX_SAMPLE_GAP:
        _coverage(conn, code, "stale", gap="quote_outside_session_or_stale")
        return False
    last = conn.execute("SELECT quote_time, payload FROM intraday_samples "
                        "WHERE code=? ORDER BY quote_time DESC LIMIT 1", (code,)).fetchone()
    gap = None
    if last:
        if sample["quote_time"] <= last["quote_time"]:
            duplicate = sample["quote_time"] == last["quote_time"]
            _coverage(conn, code, "duplicate" if duplicate else "late",
                      gap=None if duplicate else "out_of_order")
            return False
        previous = _stamp(last["quote_time"])
        if _session(previous) != session:
            gap = "session_changed"
        else:
            before = json.loads(last["payload"])
            if before["source"] != sample["source"]:
                gap = "source_changed"
            elif _counter_reset(sample, before):
                gap = "cumulative_counter_reset"
            elif (ts - previous).total_seconds() > MAX_SAMPLE_GAP:
                gap = "quote_gap"
    if not last or gap == "session_changed":
        conn.execute("DELETE FROM intraday_samples WHERE quote_time < ?", (_iso(received_at - timedelta(days=3)),))
    conn.execute("INSERT INTO intraday_samples VALUES(?,?,?)", (code, sample["quote_time"], db.dumps(sample)))
    conn.execute("DELETE FROM intraday_samples WHERE code=? AND quote_time < ("
                 "SELECT quote_time FROM intraday_samples WHERE code=? ORDER BY quote_time DESC LIMIT 1 OFFSET ?)",
                 (code, code, MAX_SAMPLES - 1))
    _coverage(conn, code, "accepted", sample["quote_time"], gap)
    return True


def ingest(code, quote, received_at, source) -> bool:
    """Accept a fresh, strictly ordered provider quote, storing both provider and receipt times."""
    with db.tx() as conn:
        return _ingest(conn, code, quote, _asof(received_at), source)


def _quote_series(code, history, asof):
    session = _session(asof)
    samples = {}
    for raw in history or ():
        received = _stamp(raw.get("received_at")) if raw.get("received_at") else asof
        if not received or received > asof:
            continue
        sample = _sample(code, raw, received, raw.get("source"))
        if sample:
            t = _stamp(sample["quote_time"])
            if 0 <= (received - t).total_seconds() <= MAX_SAMPLE_GAP and session and _session(t) == session:
                samples.setdefault(t, sample)
    if not samples:
        return []
    pairs = sorted(samples.items(), reverse=True)
    if (asof - pairs[0][0]).total_seconds() > MAX_SAMPLE_GAP:
        return []
    end = 1
    for (new_t, newer), (old_t, older) in zip(pairs, pairs[1:]):
        if (newer["source"] != older["source"] or _counter_reset(newer, older)
                or (new_t - old_t).total_seconds() > MAX_SAMPLE_GAP):
            break
        end += 1
    return list(reversed(pairs[:end]))


def _quote_window(pairs, seconds):
    last_t, last = pairs[-1]
    threshold = last_t - timedelta(seconds=seconds)
    previous = next(((t, q) for t, q in reversed(pairs) if t <= threshold), None)
    if not previous or (threshold - previous[0]).total_seconds() > MAX_SAMPLE_GAP:
        return None
    old = previous[1]
    volume = last["vol"] - old["vol"] if last["vol"] is not None and old["vol"] is not None else None
    amount = last["amount"] - old["amount"] if last["amount"] is not None and old["amount"] is not None else None
    return {"seconds": int((last_t - previous[0]).total_seconds()),
            "return_pct": round((last["price"] / old["price"] - 1) * 100, 4),
            "volume": volume if volume is not None and volume >= 0 else None,
            "vwap": round(amount / volume, 4) if volume and volume > 0 and amount and amount > 0 else None}


def _feeds(minutes):
    if not minutes:
        return {}
    if isinstance(minutes, list):
        return {"m1": {"rows": minutes, "bar_time": "close", "source": "provided"}}
    if "rows" in minutes:
        return {minutes.get("period", "m1"): minutes}
    return minutes


def _completed(feed, step, asof):
    if not feed or not _session(asof):
        return []
    published = _stamp(feed.get("available_at"))
    if published and published > asof:
        return []
    start, end = _session(asof)
    morning_start = start.replace(hour=9, minute=30)
    morning_end = start.replace(hour=11, minute=30)
    afternoon = start.hour == 13
    slots = {}
    for raw in feed.get("rows", []):
        t = _stamp(raw.get("date") or raw.get("time"))
        close = _number(raw.get("close"), positive=True)
        if not t or close is None or t.second or t.microsecond:
            continue
        if feed.get("bar_time") != "close":
            t += timedelta(minutes=step)
        bar_session_start = morning_start if afternoon and morning_start < t <= morning_end else start
        if not bar_session_start < t <= min(end, asof) or int((t - bar_session_start).total_seconds()) % (60 * step):
            continue
        row = {"time": t, "close": close, **{k: _number(raw.get(k), positive=k != "vol")
                                             for k in ("high", "low", "vol")}}
        slots[t] = row if t not in slots or slots[t] == row else None
    rows = []
    if slots:
        t = max(slots)
        if (asof - t).total_seconds() <= 60 * (step + 1):
            while slots.get(t):
                rows.append(slots[t])
                t -= timedelta(minutes=step)
                if afternoon and t == start:
                    t = morning_end  # 交易所午休，无交易分钟，不插值。
    return rows[::-1]


def _context_stamp(value, asof):
    if isinstance(value, str) and len(value) == 8 and value[2:3] == ":":
        value = f"{asof:%Y-%m-%d}T{value}"
    return _stamp(value)


def _market_input(context, asof):
    if not context:
        return None
    t = _context_stamp(context.get("observed_at") or context.get("time"), asof)
    for value in (t, *(_context_stamp(context[k], asof) for k in ("index_data_at", "breadth_data_at") if k in context)):
        if not value or _session(value) != _session(asof) or not 0 <= (asof - value).total_seconds() <= 120:
            return None
    index = next((i for i in context.get("indices", []) if i.get("code") == "sh000300"), None)
    if index is not None and not index.get("live"):
        return None
    index = index if index is not None else context
    speed = _number(index.get("speed5"))
    width = _number(context.get("up_ratio"))
    if speed is None or width is None or not 0 <= width <= 1:
        return None
    if "breadth" in context and (context["breadth"] or {}).get("up") is None:
        return None
    events = index.get("events") or []
    risk = bool(context.get("risk_off") or "异动跳水" in events or speed <= -0.4 or width < 0.15)
    turning = "V形反转" in events or "异动拉升" in events
    fading = "冲高回落" in events or speed < -0.15
    return {"time": _iso(t), "speed5": speed, "up_ratio": width, "risk_off": risk,
            "turning": turning, "fading": fading, "above_vwap": index.get("above_vwap"),
            "index_pct": _number(index.get("pct"))}


def _macd(rows):
    dif, dea, hist = macd([r["close"] for r in rows])
    return {"dif": round(dif[-1], 7), "dea": round(dea[-1], 7), "hist": round(hist[-1], 7),
            "cross": "up" if hist[-2] <= 0 < hist[-1] else "down" if hist[-2] >= 0 > hist[-1] else None,
            "hist_delta": round(hist[-1] - hist[-2], 7),
            "hist_shape": "expanding" if abs(hist[-1]) > abs(hist[-2]) else
                          "contracting" if abs(hist[-1]) < abs(hist[-2]) else "flat",
            "above_zero": dif[-1] > 0, "bar_time": _iso(rows[-1]["time"]), "samples": len(rows)}


def _structure(rows1, rows5, price):
    recent = [r["close"] for r in rows1[-25:]]
    valid5 = len(rows5) >= 5 and all(r["low"] and r["high"] and r["low"] <= r["close"] <= r["high"]
                                  for r in rows5[-5:])
    if valid5:
        low = min(r["low"] for r in rows5[-5:])
        high = max(r["high"] for r in rows5[-5:])
    else:
        low, high = min(recent), max(recent)
    moves = [abs(b - a) for a, b in zip(recent, recent[1:])]
    typical = max(median(moves), 0.01)
    width = min(max(0.01, typical * 2), max((high - low) * 0.2, 0.01))
    trend = median(recent[-5:]) - median(recent[-15:-5])
    prior_high, prior_low = max(recent[:-5]), min(recent[:-5])
    target_high, target_low = max(recent[-5:]), min(recent[-5:])
    if (target_high > prior_high + width and abs(price - prior_high) <= width):
        setup, side, anchor, target = "breakout_retest", "buy", prior_high, target_high
    elif (target_low < prior_low - width and abs(price - prior_low) <= width):
        setup, side, anchor, target = "breakout_retest", "sell", prior_low, target_low
    elif trend > width and abs(price - median(recent[-12:-3])) <= width:
        setup, side, anchor, target = "trend_pullback", "buy", median(recent[-12:-3]), high
    elif trend < -width and abs(price - median(recent[-12:-3])) <= width:
        setup, side, anchor, target = "trend_pullback", "sell", median(recent[-12:-3]), low
    elif price <= low + width and price >= low - width:
        setup, side, anchor, target = "range_edge", "buy", low, high
    elif price >= high - width and price <= high + width:
        setup, side, anchor, target = "range_edge", "sell", high, low
    else:
        setup = side = anchor = target = None
    return setup, side, anchor, target, width, typical, {
        "swing_low": round(low, 4), "swing_high": round(high, 4), "basis": "m5_ohlc" if valid5 else "m1_closes",
        "trend": round(trend, 4), "typical_minute_move": round(typical, 4)}


def evaluate(code, quotes_history, minute_rows, market_context, variant="balanced", asof=None) -> dict:
    """Evaluate a single direction from information available at asof. No data or order writes."""
    window, min_volume, horizon, lifetime = VARIANTS[variant]
    t = _asof(asof)
    out = {"version": VERSION, "variant": variant, "code": code, "phase": "unknown", "side": None, "setup": None,
           "quote_time": None, "evaluated_at": _iso(t), "expires_at": None, "entry_zone": None, "target": None,
           "invalidation": None, "evidence": [], "missing": [], "reason": None, "features": {}}

    def stop(reason, *missing):
        out.update(reason=reason, missing=list(missing))
        return out

    session = _session(t)
    if not session:
        return stop("out_of_continuous_session", "session")
    pairs = _quote_series(code, quotes_history, t)
    if pairs:
        out["quote_time"] = pairs[-1][1]["quote_time"]
    market = _market_input(market_context, t)
    if not market:
        return stop("market_context_unavailable", "market_context")
    out["features"]["market"] = market
    if market["risk_off"]:
        out.update(phase="invalid", setup="risk_exit", evidence=["market_risk_off"])
        return stop("market_risk_off")
    if not pairs:
        return stop("quote_missing_or_stale", "fresh_quote")
    quote = pairs[-1][1]
    price = quote["price"]
    w30, w60 = _quote_window(pairs, 30), _quote_window(pairs, 60)
    active_window = w30 if window == 30 else w60
    vwap = quote["amount"] / quote["vol"] if quote["amount"] and quote["vol"] else None
    relative = quote["pct"] - market["index_pct"] if quote["pct"] is not None and market["index_pct"] is not None else None
    out["features"].update(price=price, source=quote["source"], received_at=quote["received_at"], w30=w30, w60=w60,
                           vwap=round(vwap, 4) if vwap else None,
                           relative_to_index=round(relative, 4) if relative is not None else None)
    feeds = _feeds(minute_rows)
    rows1 = _completed(feeds.get("m1"), 1, t)
    if len(rows1) < 60:
        out["features"].update(m1_samples=len(rows1), m1_earliest=_iso(t.replace(hour=10, minute=30, second=0, microsecond=0)))
        return stop("macd_warming_up", "60_consecutive_closed_m1")
    if not active_window:
        return stop("quote_window_incomplete", f"continuous_{window}s")
    rows5 = _completed(feeds.get("m5"), 5, t)
    momentum = _macd(rows1)
    out["features"].update(macd=momentum, m1_source=feeds["m1"].get("source"),
                           m1_sample_type=feeds["m1"].get("sample_type", "completed_minute_close"),
                           m5_source=(feeds.get("m5") or {}).get("source") if rows5 else None)
    setup, side, anchor, target, width, typical, structure = _structure(rows1, rows5, price)
    out["features"]["structure"] = structure
    if not side:
        out.update(phase="watch", evidence=["completed_minutes", "covered_quote_window"])
        return stop("no_structural_entry")
    direction = 1 if side == "buy" else -1
    zone = [round(anchor - width, 4), round(anchor + width, 4)]
    invalidation = round(anchor - direction * width * 1.5, 4)
    minutes_left = (session[1] - t).total_seconds() / 60
    potential = direction * (target - price)
    reach = typical * math.sqrt(min(minutes_left, horizon)) * 1.8
    fee = _number((market_context.get("research") or {}).get("round_trip_cost_pct"), positive=True)
    cost = (fee if fee is not None else 0.4) / 100
    out.update(phase="candidate", side=side, setup=setup, expires_at=_iso(min(t + timedelta(seconds=lifetime), session[1])),
               entry_zone=zone, target=round(target, 4), invalidation=invalidation,
               evidence=[setup, "completed_minutes", "covered_quote_window"])
    out["features"].update(remaining_minutes=round(minutes_left, 2), reach_allowance=round(reach, 4),
                           research_round_trip_cost_pct=round(cost * 100, 4))
    if potential <= max(price * cost, 0.02) or potential > reach:
        out.update(phase="watch")
        return stop("net_space_too_small" if potential <= max(price * cost, 0.02) else "target_outside_research_reach")
    volumes = [r["vol"] for r in rows1[-13:-1] if r["vol"] is not None and r["vol"] >= 0]
    last_vol = rows1[-1]["vol"]
    ratio = last_vol / median(volumes) if len(volumes) >= 8 and median(volumes) > 0 and last_vol is not None else None
    out["features"]["minute_volume_ratio"] = round(ratio, 3) if ratio is not None else None
    price_turn = active_window["return_pct"] * direction > 0
    macd_turn = (momentum["cross"] == ("up" if direction > 0 else "down") or
                 momentum["hist_delta"] * direction > 0 and
                 (momentum["hist"] * direction > 0 or momentum["hist_shape"] == "contracting"))
    volume_ok = ratio is not None and ratio >= min_volume and (active_window["volume"] or 0) > 0
    backdrop_ok = (not market["fading"] or market["turning"]) if side == "buy" else not (
        market["turning"] and market["speed5"] > 0.2 and market["up_ratio"] > 0.65)
    if relative is not None and not (market["turning"] or market["fading"]):
        backdrop_ok = backdrop_ok and direction * relative >= -0.5
    rolling_vwap = active_window["vwap"]
    vwap_ok = rolling_vwap is None or direction * (price - rolling_vwap) >= -width / 2
    if setup == "trend_pullback" and vwap is not None:
        vwap_ok = vwap_ok and direction * (anchor - vwap) >= -width
    if price_turn and macd_turn and volume_ok and backdrop_ok and vwap_ok and zone[0] <= price <= zone[1]:
        out.update(phase="confirmed", evidence=out["evidence"] + ["directional_quote_move", "minute_macd_turn",
                                                                "minute_volume", "market_backdrop"])
        if rolling_vwap is not None:
            out["evidence"].append("rolling_vwap_alignment")
        return stop("structure_and_turn_confirmed")
    out.update(phase="watch" if price_turn or macd_turn else "candidate")
    reason = "market_conflict" if not backdrop_ok else "vwap_conflict" if not vwap_ok else "waiting_for_direction_volume_and_macd"
    return stop(reason, *([] if ratio is not None else ["minute_volume"]))


def _revision(conn, record, identity, revision):
    item = {**record, "opportunity_id": identity, "revision": revision}
    conn.execute("INSERT INTO intraday_revisions VALUES(?,?,?,?,?,?)",
                 (identity, revision, item["code"], item["variant"], item["evaluated_at"], db.dumps(item)))
    return item


def _transition(conn, new, previous):
    eligible = new["phase"] in ACTIVE and bool(new["side"])
    was_active = previous and previous["phase"] in ACTIVE and previous.get("opportunity_id")
    if was_active:
        now_t = _stamp(new["evaluated_at"])
        expires = _stamp(previous["expires_at"])
        price = new["features"].get("price")
        sign = 1 if previous["side"] == "buy" else -1
        crossed = price is not None and (sign * (price - previous["invalidation"]) <= 0 or
                                        sign * (price - previous["target"]) >= 0)
        same = (previous["setup"], previous["side"]) == (new["setup"], new["side"])
        if not eligible or not same or crossed or now_t >= expires:
            reason = ("opportunity_expired" if now_t >= expires else "structure_crossed" if crossed
                      else "structure_replaced" if eligible and not same else new["reason"])
            terminal = {**previous, "phase": "invalid", "reason": reason, "evaluated_at": new["evaluated_at"],
                        "quote_time": new["quote_time"], "features": new["features"], "missing": new["missing"],
                        "evidence": new["evidence"]}
            return _revision(conn, terminal, previous["opportunity_id"], previous["revision"] + 1)
        new["expires_at"] = previous["expires_at"]
        fields = ("phase", "entry_zone", "target", "invalidation", "reason")
        if all(new[k] == previous[k] for k in fields):
            return {**new, "opportunity_id": previous["opportunity_id"], "revision": previous["revision"]}
        return _revision(conn, new, previous["opportunity_id"], previous["revision"] + 1)
    if eligible:
        seed = "|".join(str(new[k]) for k in ("code", "variant", "quote_time", "evaluated_at", "setup", "side"))
        identity = hashlib.sha256(seed.encode()).hexdigest()[:24]
        return _revision(conn, new, identity, 1)
    return {**new, "opportunity_id": None, "revision": None}


def observe(code, quote, minutes, market_context, asof=None, *, source=None) -> dict:
    """Ingest and evaluate all versions against one snapshot; publish latest and append transitions."""
    t = _asof(asof)
    quote = quote or {}
    received = _stamp(quote["received_at"]) if "received_at" in quote else t
    source = quote.get("source") or source
    with db.tx() as conn:
        if received is None:
            _coverage(conn, code, "missing", gap="quote_receipt_time_invalid")
        elif received > t:
            _coverage(conn, code, "stale", gap="quote_receipt_after_evaluation")
        else:
            _ingest(conn, code, quote, received, source)
        oldest = _iso(t - timedelta(minutes=3))
        samples = [json.loads(r["payload"]) for r in conn.execute(
            "SELECT payload FROM intraday_samples WHERE code=? AND quote_time>=? ORDER BY quote_time",
            (code, oldest))]
        result, changed = {}, False
        for variant in VARIANTS:
            row = conn.execute("SELECT payload FROM intraday_latest WHERE code=? AND variant=?", (code, variant)).fetchone()
            prev = json.loads(row["payload"]) if row else None
            if prev and prev["evaluated_at"] >= _iso(t):
                result[variant] = prev
                continue
            current = _transition(conn, evaluate(code, samples, minutes, market_context, variant, t), prev)
            conn.execute("INSERT OR REPLACE INTO intraday_latest VALUES(?,?,?)", (code, variant, db.dumps(current)))
            result[variant] = current
            changed = True
        if changed:
            _coverage(conn, code, "evaluation", _iso(t), result["balanced"]["reason"])
        return result


def latest(codes=None) -> dict:
    if codes is not None:
        codes = list(dict.fromkeys(codes))
        if not codes:
            return {}
    args = () if codes is None else tuple(codes)
    query = "SELECT * FROM intraday_latest"
    if codes is not None:
        query += " WHERE code IN (" + ",".join("?" for _ in args) + ")"
    result = {}
    for row in db.q(query, args):
        result.setdefault(row["code"], {})[row["variant"]] = json.loads(row["payload"])
    return result


def status() -> dict:
    counters = db.q1("SELECT COUNT(*) AS codes, MAX(last_quote) AS last_quote, MAX(last_eval) AS last_eval, "
                     "SUM(accepted) AS accepted, SUM(duplicate) AS duplicate, SUM(late) AS late, SUM(stale) AS stale, "
                     "SUM(missing) AS missing, SUM(resets) AS resets FROM intraday_coverage")
    gaps = {row["last_gap"]: row["n"] for row in db.q(
        "SELECT last_gap, COUNT(*) AS n FROM intraday_coverage WHERE last_gap IS NOT NULL GROUP BY last_gap")}
    return {"version": VERSION, **{k: (v or 0) if k not in ("last_quote", "last_eval") else v
                                  for k, v in counters.items()}, "gaps": gaps,
            "stored_samples": db.q1("SELECT COUNT(*) AS n FROM intraday_samples")["n"],
            "opportunity_revisions": db.q1("SELECT COUNT(*) AS n FROM intraday_revisions")["n"]}


_inputs_lock = threading.Lock()
_bars, _due, _busy = {}, {}, set()
_backdrop = {"data": {}, "due": None, "busy": False}


def _tape_feed(tape):
    date = str(tape.get("date") or "").replace("-", "")
    if len(date) != 8 or not date.isdecimal():
        return {}
    date = f"{date[:4]}-{date[4:6]}-{date[6:]}"
    return {"rows": [{"date": date + "T" + p["t"], "close": p["p"], "vol": p.get("v")}
                     for p in tape.get("points", [])], "source": tape.get("source"),
            "bar_time": "start", "sample_type": "minute_price_sample", "available_at": _iso(now())}


def _update_inputs(code):
    from . import market, marketstate

    try:
        if code is None:
            try:
                context = marketstate.state()
                index_rows = _tape_feed(market.intraday("sh000300")).get("rows", [])
                if not MOCK:
                    context = {**context, "index_data_at": index_rows[-1]["date"] if index_rows else None,
                               "breadth_data_at": market.SNAP_INFO.get("time")}
            except (RuntimeError, ValueError, TypeError, KeyError):
                context = {}
            with _inputs_lock:
                _backdrop["data"] = context
            return
        feeds = {}
        try:
            feeds["m1"] = _tape_feed(market.intraday(code))
        except (RuntimeError, ValueError, TypeError, KeyError):
            pass
        try:
            data = market.kline_period(code, "m5", 96)
            feeds["m5"] = {**data, "available_at": _iso(now())}
        except (RuntimeError, ValueError, TypeError, KeyError):
            pass
        with _inputs_lock:
            _bars.setdefault(code, {}).update(feeds)
    finally:
        with _inputs_lock:
            if code is None:
                _backdrop["busy"] = False
            else:
                _busy.discard(code)


def _refresh_inputs(codes, t):
    with _inputs_lock:
        for code in set(_bars) - set(codes) - _busy:
            _bars.pop(code, None)
            _due.pop(code, None)
        if not _backdrop["busy"] and (_backdrop["due"] is None or _backdrop["due"] <= t):
            _backdrop.update(busy=True, due=t + timedelta(seconds=30))
            threading.Thread(target=_update_inputs, args=(None,), daemon=True, name="aq5-intraday-market").start()
        ready = sorted((c for c in codes if c not in _busy and _due.get(c, t) <= t), key=lambda c: _due.get(c, t))
        for code in ready[:max(0, 4 - len(_busy))]:
            _busy.add(code)
            _due[code] = t + timedelta(seconds=25)
            threading.Thread(target=_update_inputs, args=(code,), daemon=True, name="aq5-intraday-bars").start()


def runtime_tick(codes, *, return_quotes=False) -> dict:
    """Sample a bounded code batch and refresh minute and index context in the background."""
    from . import market

    codes = list(dict.fromkeys(market.norm_code(code) for code in codes if code))[:40]
    if not codes or not _session(now()):
        return {"signals": {}, "quotes": {}, "asof": _iso(now())} if return_quotes else {}
    _refresh_inputs(codes, now())
    try:
        quotes = market.quotes_fast(codes)
    except (RuntimeError, ValueError, TypeError, KeyError):
        quotes = {}
    t = now()
    with _inputs_lock:
        context = dict(_backdrop["data"])
        feeds = {code: dict(_bars.get(code, {})) for code in codes}
    quotes = {code: {**q, "source": "模拟" if MOCK else q.get("source") or "腾讯",
                     "received_at": q.get("received_at", _iso(t))} for code, q in quotes.items()}
    signals = {code: observe(code, quotes.get(code), feeds[code], context, t,
                             source=(quotes.get(code) or {}).get("source")) for code in codes}
    if return_quotes:
        return {"signals": signals, "quotes": quotes, "asof": _iso(t)}
    return signals

