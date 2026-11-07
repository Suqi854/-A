"""Optional personal-research TDX quotes. Never manufacture a trading date.

eltdx 3.3.5 is kept in the project's vendor/tdx directory. Only full depth
records are used; a same-server market date and per-symbol daily bar corroborate
the date. Tencent supplies explicitly labelled same-day security/limit metadata.
"""
from __future__ import annotations

import importlib.util
import math
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from datetime import date, datetime, time as day_time

from .config import CN_TZ, DATA, ROOT, now

TIME_BASIS = "tdx_depth_hhmmss_with_verified_market_date"
HOSTS = ("116.205.183.150:7709", "116.205.171.132:7709")
VENDOR = ROOT / "vendor" / "tdx"
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="aq5-tdx")
_lock = threading.Lock()
_future = None
_client = None
_host_index = 0
_dates = {}
_retry_at = 0.0
_state = {"state": "idle", "last_ok": None, "last_error": None, "failures": 0,
          "rejected": 0, "rejection_reasons": {}, "time_basis": TIME_BASIS}


def supported(code):
    return bool(re.fullmatch(r"(?:sh(?:600|601|603|605)|sz(?:000|001|002|003))\d{3}", code))


def _number(value, positive=False):
    if isinstance(value, bool):
        raise ValueError("invalid_number")
    try:
        value = float(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError("invalid_number") from exc
    if not math.isfinite(value) or value < 0 or (positive and value <= 0):
        raise ValueError("invalid_number")
    return value


def fresh(stamp, received_at):
    if stamp > received_at or stamp.date().weekday() >= 5:
        return False
    if received_at.weekday() < 5 and day_time(9, 15) <= received_at.time().replace(tzinfo=None) < day_time(15):
        return stamp.date() == received_at.date() and (received_at - stamp).total_seconds() <= 12
    return stamp.time().replace(tzinfo=None) >= day_time(15) and 0 <= (received_at.date() - stamp.date()).days <= 7


def normalize_record(record, dates, bar_date, anchor, received_at):
    code = str(record.get("exchange", "")) + str(record.get("code", ""))
    if not supported(code) or anchor.get("code") != code:
        raise ValueError("unsupported_or_wrong_code")
    if anchor.get("suspended"):
        raise ValueError("suspended_security")
    if record.get("market_id") != (1 if code.startswith("sh") else 0):
        raise ValueError("wrong_market")
    if len(dates) != 2 or not all(type(d) is date for d in dates) or dates[0] != dates[1] or bar_date != dates[0]:
        raise ValueError("market_date_not_verified")
    raw = record.get("update_time_raw")
    if type(raw) is not int or not 0 < raw <= 235959:
        raise ValueError("invalid_depth_time")
    clock = day_time(raw // 10000, raw // 100 % 100, raw % 100)
    stamp = datetime.combine(dates[0], clock, CN_TZ)
    if not fresh(stamp, received_at):
        raise ValueError("stale_or_future_quote")
    anchor_stamp = datetime.strptime(str(anchor.get("time", "")), "%Y%m%d%H%M%S").replace(tzinfo=CN_TZ)
    if anchor_stamp.date() != stamp.date() or anchor_stamp > stamp or not anchor.get("name"):
        raise ValueError("same_day_metadata_missing_or_newer")
    price, prev, opening, high, low = (_number(record[k], True) for k in
        ("last_price", "last_close_price", "open_price", "high_price", "low_price"))
    if not low <= min(price, opening) <= max(price, opening) <= high:
        raise ValueError("invalid_ohlc")
    up, down = _number(anchor.get("up_limit"), True), _number(anchor.get("dn_limit"), True)
    if abs(prev - _number(anchor.get("prev"), True)) > 0.001 or not down <= low <= high <= up:
        raise ValueError("limit_metadata_conflict")
    hands, amount = _number(record["total_hand"], True), _number(record["amount"], True)
    book = []
    for key, reverse in (("buy_levels", True), ("sell_levels", False)):
        levels = record.get(key) or []
        if len(levels) != 5:
            raise ValueError("incomplete_depth")
        rows = [[_number(x["price"]), _number(x["volume"])] for x in levels]
        prices = [p for p, v in rows if p]
        if prices != sorted(prices, reverse=reverse) or any(v and not p for p, v in rows):
            raise ValueError("invalid_depth")
        book.append(rows)
    if book[0][0][0] and book[1][0][0] and book[0][0][0] > book[1][0][0]:
        raise ValueError("crossed_depth")
    if not book[0][0][0] and not book[1][0][0]:
        raise ValueError("no_executable_depth")
    return {"code": code, "name": anchor["name"], "price": price, "prev": prev, "open": opening,
            "high": high, "low": low, "chg": round(price-prev, 4), "pct": (price/prev-1)*100,
            "vol": hands*100, "amount": amount, "avg": amount/(hands*100),
            "bids": book[0], "asks": book[1], "outer": None, "inner": None,
            "up_limit": up, "dn_limit": down, "time": stamp.strftime("%Y%m%d%H%M%S"),
            "quote_time": stamp.isoformat(timespec="seconds"), "received_at": received_at.isoformat(timespec="milliseconds"),
            "source": "通达信", "time_basis": TIME_BASIS,
            "metadata_source": "腾讯（同交易日证券名称及涨跌停价）", "metadata_time": anchor["time"],
            "quote_time_quality": "derived_market_date", "depth_volume_unit": "hand",
            "source_market_date": dates[0].isoformat(), "source_time_raw": raw}


def available():
    return (VENDOR / "eltdx" / "__init__.py").is_file() or importlib.util.find_spec("eltdx") is not None


def status():
    return {**_state, "rejection_reasons": dict(_state["rejection_reasons"]),
            "available": available(), "cooldown_seconds": max(0, round(_retry_at-time.monotonic()))}


def _connect():
    global _client
    # Set the package's data directory before importing it; never use LOCALAPPDATA.
    os.environ["ELTDX_DATA_DIR"] = str(DATA / "tdx-cache")
    if (VENDOR / "eltdx").is_dir() and str(VENDOR) not in sys.path:
        sys.path.insert(0, str(VENDOR))
    from eltdx import TdxClient
    if _client is None:
        _client = TdxClient(host=HOSTS[_host_index], timeout=0.7, probe_hosts=False,
                            heartbeat_interval=None, server_count=1, connections_per_server=1, runtime_workers=1)
        _client.connect()
    return _client


def _fetch(codes, metadata):
    global _client, _host_index, _retry_at
    try:
        client = _connect()
        from eltdx.serialization import to_jsonable
        handshake = client.session.handshake()
        dates = (handshake.server_date_1, handshake.server_date_2)
        if len(set(dates)) != 1 or type(dates[0]) is not date:
            raise ValueError("market_date_conflict")
        page = to_jsonable(client.quotes.get_depth(codes))
        received_at = now()
        records = page.get("records", [])
        actual = [str(r.get("exchange", ""))+str(r.get("code", "")) for r in records]
        if len(actual) != len(set(actual)) or set(actual) - set(codes):
            raise ValueError("unexpected_or_duplicate_codes")
        result = {}
        for record, code in zip(records, actual):
            try:
                key = (HOSTS[_host_index], dates[0], code)
                if key not in _dates:
                    bars = client.bars.get(code, period="day", count=1, adjust="none").bars
                    if len(bars) != 1:
                        raise ValueError("missing_date_bar")
                    if bars[0].time.date() != dates[0]:
                        raise ValueError("market_date_not_verified")
                    _dates[key] = bars[0].time.date()
                    if len(_dates) > 500:
                        _dates.clear()
                        _dates[key] = bars[0].time.date()
                result[code] = normalize_record(record, dates, _dates[key], metadata[code], received_at)
            except (ValueError, TypeError, KeyError, OverflowError) as exc:
                _state["rejected"] += 1
                reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
                _state["rejection_reasons"][reason] = _state["rejection_reasons"].get(reason, 0)+1
        _state.update(state="ready" if result else "no_eligible_quotes", last_response_at=received_at.isoformat(), last_error=None)
        if result:
            _state["last_ok"] = received_at.isoformat()
        return result
    except Exception as exc:  # Optional provider failure must never take down the existing quotes.
        _state.update(state="unavailable", last_error=type(exc).__name__, failures=_state["failures"]+1)
        _retry_at = time.monotonic()+30
        if _client is not None:
            try:
                _client.close()
            except Exception:
                pass
        _client = None
        _dates.clear()
        _host_index = (_host_index+1) % len(HOSTS)
        return {}


def quotes(codes, metadata, timeout=1.6):
    """One bounded worker, no unbounded queue; failed/cold/busy requests fall back."""
    global _future
    wanted = list(dict.fromkeys(c for c in codes if supported(c) and c in metadata))[:60]
    if not wanted:
        _state["state"] = "waiting_metadata"
        return {}
    if not available() or time.monotonic() < _retry_at:
        return {}
    with _lock:
        if _future is not None and not _future.done():
            return {}
        _future = _executor.submit(_fetch, wanted, {c: dict(metadata[c]) for c in wanted})
        pending = _future
    try:
        return pending.result(timeout=max(0, min(timeout, 1.6)))
    except TimeoutError:
        _state["state"] = "warming_or_timeout"
        return {}
