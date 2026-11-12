"""Observed-time public market adapters with explicit price-basis evidence."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import statistics
import threading
from datetime import datetime, timedelta

from .. import market, db
from ..config import CN_TZ, MOCK, now
from .features import stamp, finite
from .calendar import official_days

_cache, _adjustments, _context_cache = {}, {}, {}
_lock = threading.Lock()
PERIODS = {"day": 0, "60m": 60, "15m": 15, "5m": 5}
PRICE_FIELDS = ("open", "high", "low", "close")


class AdjustmentError(ValueError):
    def __init__(self, attempts):
        self.attempts = attempts
        super().__init__('adjustment_sources_unavailable:' + ';'.join(a['source'] + '_' + a['reason'] for a in attempts))


def _source_reason(exc):
    if isinstance(exc, market.Blocked):
        return 'blocked'
    if isinstance(exc, json.JSONDecodeError):
        return 'invalid_json'
    if isinstance(exc, ValueError) and str(exc).replace('_', '').isalnum() and len(str(exc)) < 80:
        return str(exc)
    return 'request_failed' if isinstance(exc, RuntimeError) else 'invalid_payload'


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _ends(day, minutes):
    base = datetime.fromisoformat(day).replace(tzinfo=CN_TZ)
    return [base.replace(hour=h, minute=m) + timedelta(minutes=i)
            for h, m in ((9, 30), (13, 0)) for i in range(minutes, 121, minutes)]


def _expected_end(t, minutes):
    days = official_days(t.year)
    if not days:
        return None
    choices = []
    for day in days:
        if day > t.date().isoformat():
            break
        if minutes:
            choices.extend(x for x in _ends(day, minutes) if x <= t)
        else:
            end = datetime.fromisoformat(day).replace(hour=15, tzinfo=CN_TZ)
            if end <= t:
                choices.append(end)
    return max(choices) if choices else None


def normalize(feed, timeframe, asof):
    """Filter only completed, actually available revisions; never refresh known_at."""
    t = stamp(asof)
    if not t or timeframe not in PERIODS:
        raise ValueError("invalid period/asof")
    rows, issues = [], set()
    minutes = PERIODS[timeframe]
    observed = stamp(feed.get("observed_at"))
    evidence = feed.get("adjustment_evidence") or {}
    verified = (feed.get("adjustment_verified") is True and evidence.get("verified") is True
                and bool(evidence.get("input_hash")) and stamp(evidence.get("observed_at")) is not None)
    for raw in feed.get("rows", []):
        end = stamp(raw.get("date"))
        if not end:
            issues.add("invalid_timestamp"); continue
        if not minutes:
            end = end.replace(hour=15, minute=0, second=0, microsecond=0)
        elif feed.get("bar_time") == "start":
            end += timedelta(minutes=minutes)
        elif feed.get("bar_time") != "end":
            issues.add("bar_timestamp_basis_unknown"); continue
        if end > t or raw.get("complete") is False:
            continue  # A current forming bar is not a broken completed bar.
        available = stamp(raw.get("available_at")) or observed
        if not available:
            issues.add("availability_unknown"); continue
        if available > t:
            issues.add("not_available_at_asof"); continue
        if available < end:
            issues.add("available_before_bar_end"); continue
        if minutes and end not in _ends(end.date().isoformat(), minutes):
            issues.add("invalid_session_bar_end"); continue
        if any(not finite(raw.get(k)) for k in (*PRICE_FIELDS, "vol")):
            issues.add("invalid_ohlcv"); continue
        if raw["low"] <= 0 or raw["vol"] < 0 or raw["high"] < max(raw["close"], raw["open"]) or raw["low"] > min(raw["close"], raw["open"]):
            issues.add("invalid_ohlcv"); continue
        rows.append({**raw, "date": end.isoformat(), "complete": True, "available_at": available.isoformat(),
                     "source": feed.get("source"), "adjustment": feed.get("adjustment", "unknown"),
                     "adjustment_verified": verified, "adjustment_asof": feed.get("adjustment_asof")})
    rows.sort(key=lambda r: r["date"])
    if len({r["date"] for r in rows}) != len(rows):
        issues.add("duplicate_bars")
    if rows:
        expected = _expected_end(t, minutes)
        if expected is None:
            issues.add("calendar_unavailable")
        elif stamp(rows[-1]["date"]) != expected:
            issues.add("stale_completed_bar")
        first, last = stamp(rows[0]["date"]), stamp(rows[-1]["date"])
        actual = {r["date"] for r in rows}
        # Validate gaps on known exchange-calendar years, including lunch and
        # missing entire sessions; the leading partial source window is allowed.
        for year in range(first.year, last.year + 1):
            for day in official_days(year):
                if not first.date().isoformat() <= day <= last.date().isoformat():
                    continue
                ends = _ends(day, minutes) if minutes else [datetime.fromisoformat(day).replace(hour=15, tzinfo=CN_TZ)]
                if any(first <= e <= last and e.isoformat() not in actual for e in ends):
                    issues.add("missing_bars")
    if len(rows) < 60:
        issues.add("insufficient_completed_bars")
    if not feed.get("source"):
        issues.add("source_unknown")
    if not verified:
        issues.add("adjustment_unverified")
    if verified and (stamp(evidence["observed_at"]) > t or stamp(feed.get("adjustment_asof")) is None
                     or stamp(feed["adjustment_asof"]) > t):
        issues.add("adjustment_not_available_at_asof")
    if feed.get("input_issue"):
        issues.add(feed["input_issue"])
    for row in rows:
        row["continuity_verified"] = "missing_bars" not in issues
    return {"rows": rows, "source": feed.get("source"), "timeframe": timeframe, "asof": t.isoformat(),
            "observed_at": feed.get("observed_at"), "quality": {"eligible": not issues, "reason": ",".join(sorted(issues)) or None},
            "adjustment": feed.get("adjustment"), "adjustment_evidence": copy.deepcopy(evidence), "bar_time": feed.get("bar_time"),
            "source_attempts": copy.deepcopy(feed.get('source_attempts', [])),
            "adjustment_details": copy.deepcopy(feed.get('adjustment_details', {}))}


def _daily_pair(code):
    """Tencent explicit qfq/bfq requests, including a cross-verified identity basis."""
    requested = 320
    pair = {'source': 'Tencent', 'response_fields': {}, 'response_hashes': {},
            'window': {'requested': requested, 'returned_counts': {}, 'excluded_leading_dates': {}}}
    for mode, field in (("qfq", "qfqday"), ("bfq", "day")):
        url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,{requested},{mode}"
        payload = json.loads(market.http_get(url, timeout=12, retries=1))
        node = (payload.get("data") or {}).get(code) or {}
        values = node.get(field)
        if mode == 'qfq' and not values and node.get('day'):
            field, values = 'day', node['day']
        if not isinstance(values, list) or not values:
            raise ValueError("explicit_daily_basis_missing")
        pair['response_fields'][mode] = field
        pair['response_hashes'][mode] = _hash(values)
        pair[mode] = [{"date": x[0], "open": float(x[1]), "close": float(x[2]), "high": float(x[3]),
                       "low": float(x[4]), "vol": float(x[5]) * 100} for x in values if len(x) >= 6]
        # Trim only excess leading bars, never align histories by intersection.
        pair[mode].sort(key=lambda row: row['date'])
        pair['window']['returned_counts'][mode] = len(values)
        pair['window']['excluded_leading_dates'][mode] = [r['date'] for r in pair[mode][:-requested]]
        pair[mode] = pair[mode][-requested:]
    pair["observed_at"] = now().isoformat()
    if pair['response_fields']['qfq'] == 'day':
        _verify_identity_pair(pair)
        pair['basis'] = 'qfq_request_day_crosschecked_identity'
    else:
        pair['basis'] = 'explicit_qfq'
    return pair


def _verify_identity_pair(pair):
    """A day fallback is accepted only when both completed OHLCV histories agree.

    This establishes the returned window's observed identical basis; it does not
    claim a corporate-action audit or label arbitrary raw prices as qfqday.
    """
    observed = stamp(pair['observed_at'])
    def completed(rows):
        out = {}
        for row in rows:
            end = stamp(row['date'])
            if not end:
                raise ValueError('identity_basis_invalid_date')
            if end.replace(hour=15, minute=0, second=0, microsecond=0) > observed:
                continue
            day = end.date().isoformat()
            if day in out:
                raise ValueError('identity_basis_duplicate_date')
            out[day] = row
        return out
    adjusted, plain = completed(pair['qfq']), completed(pair['bfq'])
    if not adjusted or adjusted.keys() != plain.keys():
        raise ValueError('identity_basis_dates_mismatch')
    for day, row in adjusted.items():
        raw = plain[day]
        if any(not finite(row[k]) or not finite(raw[k]) or row[k] <= 0 or raw[k] <= 0
               or abs(row[k] - raw[k]) > max(.002, row[k] * .00002) for k in PRICE_FIELDS):
            raise ValueError('identity_basis_ohlc_mismatch')
        if not finite(row['vol']) or not finite(raw['vol']) or abs(row['vol'] - raw['vol']) > 1e-6:
            raise ValueError('identity_basis_volume_mismatch')


def _eastmoney_pair(code):
    """Independently labelled same-provider fqt=1/fqt=0 fallback, full OHLC checked."""
    secid = ('1.' if code.startswith('sh') else '0.') + code[2:]
    pair = {'source': 'Eastmoney', 'basis': 'explicit_qfq', 'response_fields': {}, 'response_hashes': {}}
    for mode, fqt in (('qfq', 1), ('bfq', 0)):
        url = (f'https://push2his.eastmoney.com/api/qt/stock/kline/get?secid={secid}'
               f'&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56&klt=101&fqt={fqt}&lmt=320&end=20500101')
        payload = json.loads(market.http_get(url, 'https://quote.eastmoney.com/', timeout=8, retries=1))
        node = payload.get('data') or {}
        if str(node.get('code')) != code[2:] or not isinstance(node.get('klines'), list) or not node['klines']:
            raise ValueError('explicit_daily_basis_missing')
        values = [x.split(',') for x in node['klines']]
        pair[mode] = [{'date': x[0], 'open': float(x[1]), 'close': float(x[2]), 'high': float(x[3]),
                       'low': float(x[4]), 'vol': float(x[5]) * 100} for x in values if len(x) >= 6]
        pair['response_fields'][mode] = 'klines_fqt_' + str(fqt)
        pair['response_hashes'][mode] = _hash(node['klines'])
    pair['observed_at'] = now().isoformat()
    return pair


def _affine_evidence(pair):
    """Validate all OHLC values: dividends can require additive, not ratio, adjustment."""
    plain = {r["date"][:10]: r for r in pair["bfq"]}
    transforms = {}
    for adj in pair["qfq"]:
        day = adj["date"][:10]
        raw = plain.get(day)
        if not raw:
            continue
        x, y = [raw[k] for k in PRICE_FIELDS], [adj[k] for k in PRICE_FIELDS]
        if any(not finite(v) or v <= 0 for v in x + y):
            continue
        xm, ym = statistics.mean(x), statistics.mean(y)
        den = sum((v - xm) ** 2 for v in x)
        if den < 1e-10:
            a, b = 1.0, ym - xm
        else:
            a = sum((u - xm) * (v - ym) for u, v in zip(x, y)) / den
            b = ym - a * xm
        tolerance = max(.002, max(y) * .00002)
        residual = max(abs(a * u + b - v) for u, v in zip(x, y))
        if not .1 <= a <= 10 or residual > tolerance:
            continue
        transforms[day] = {"a": a, "b": b, "max_error": residual, "raw_low": raw["low"],
                           "raw_high": raw["high"], "raw_close": raw["close"], "adjusted_close": adj["close"]}
    if not transforms:
        raise ValueError("no_verified_adjustment_days")
    return {"verified": True, "method": "same_source_qfq_bfq_ohlc_affine",
            "source": pair.get('source'), "basis": pair.get('basis', 'explicit_qfq'),
            "response_fields": pair.get('response_fields', {}), "response_hashes": pair.get('response_hashes', {}),
            "window": copy.deepcopy(pair.get('window', {})),
            "observed_at": pair["observed_at"], "input_hash": _hash(pair), "days": transforms}


def adjustment_factors(code, t=None):
    """Return frozen per-date affine evidence, not a misleading close-price ratio."""
    current = now(); key = (code, current.date().isoformat(), int(current.timestamp()) // 300)
    with _lock:
        cached = _adjustments.get(key)
    if cached is None:
        attempts = []
        for source, loader in (('tencent', _daily_pair), ('eastmoney', _eastmoney_pair)):
            try:
                pair = loader(code)
                evidence = _affine_evidence(pair)
                observed = stamp(pair['observed_at'])
                required = {r['date'][:10] for mode in ('qfq', 'bfq') for r in pair[mode]
                            if stamp(r['date']).replace(hour=15, minute=0, second=0) <= observed}
                if not required or not required <= evidence['days'].keys():
                    raise ValueError('completed_ohlc_dates_unverified')
                attempts.append({'source': source, 'reason': 'verified'})
                cached = {"pair": pair, "evidence": evidence, 'attempts': attempts}
                break
            except (RuntimeError, ValueError, KeyError, TypeError, IndexError, AttributeError) as exc:
                attempts.append({'source': source, 'reason': _source_reason(exc)})
        if cached is None:
            raise AdjustmentError(attempts)
        with _lock:
            for old in list(_adjustments):
                if old[0] == code:
                    _adjustments.pop(old, None)
            _adjustments[key] = cached
    return copy.deepcopy(cached)


def adjustment_input(code):
    try:
        return adjustment_factors(code), None, []
    except AdjustmentError as exc:
        return None, str(exc), exc.attempts
    except (RuntimeError, ValueError, KeyError, TypeError, OSError) as exc:
        return None, 'adjustment_' + _source_reason(exc), []


def fetch(code, timeframe, asof=None, *, adjustment=None):
    """Live calls omit asof; explicit cutoffs never backdate later acquisition."""
    if timeframe not in PERIODS:
        raise ValueError("unsupported timeframe")
    requested = stamp(asof) if asof is not None else None
    if asof is not None and requested is None:
        raise ValueError('invalid asof')
    t = now(); code = market.norm_code(code); minutes = PERIODS[timeframe]
    slot = (t.date().isoformat(), t.hour if not minutes else int(t.timestamp()) // (minutes * 60))
    key = (code, timeframe, slot)
    with _lock:
        hit = copy.deepcopy(_cache.get(key))
    if hit:
        result = normalize(hit, timeframe, requested or now())
        if requested or result['quality']['eligible'] or (now() - stamp(hit['observed_at'])).total_seconds() < 30:
            return result
    period = "day" if not minutes else "m" + str(minutes)
    if MOCK:
        raw = {**market.kline_period(code, period, 320), "adjustment": "unknown", "adjustment_verified": False,
               "bar_time": "end", "observed_at": now().isoformat()}
    else:
        bundle, adjustment_failure, attempts = adjustment if adjustment is not None else adjustment_input(code)
        if not minutes:
            if bundle:
                pair = bundle['pair']
                raw = {"rows": pair["qfq"], "source": (pair.get('source') or 'verified provider') + ' ' + pair.get('basis', 'explicit_qfq')}
            else:
                rows, source = market._try_sources("forecast day " + code, [("Sina raw daily", lambda: market._kline_sina(code, 320, 240))])
                raw = {"rows": rows, "source": source}
        else:
            rows, source = market._try_sources("forecast minute " + code, [
                ("Tencent raw minute", lambda: market._kline_tencent_m(code, 320, minutes)),
                ("Sina raw minute", lambda: market._kline_sina(code, 320, minutes))])
            raw = {"rows": rows, "source": source}
        raw.update(bar_time="end", adjustment="unknown", adjustment_verified=False)
        raw['source_attempts'] = bundle.get('attempts', []) if bundle else attempts
        if adjustment_failure:
            raw['input_issue'] = adjustment_failure
        cutoff = now()
        raw['rows'] = [r for r in raw['rows'] if stamp(r.get('date')) and
                       (stamp(r['date']) if minutes else stamp(r['date']).replace(hour=15, minute=0, second=0)) <= cutoff]
        if bundle:
            ev = bundle["evidence"]
            dates = {str(r.get("date", ""))[:10] for r in raw["rows"] if stamp(r.get("date")) and stamp(r["date"]).date() <= t.date()}
            all_verified = bool(dates) and dates <= ev["days"].keys()
            if not all_verified:
                raw['adjustment_details'] = {'reason': 'adjustment_date_coverage_missing', 'dates': sorted(dates - ev['days'].keys())}
            if minutes and all_verified:
                converted = []
                for row in raw["rows"]:
                    f = ev["days"].get(row["date"][:10])
                    if not f or row["low"] < f["raw_low"] - .02 or row["high"] > f["raw_high"] + .02:
                        raw['adjustment_details'] = {'reason': 'minute_raw_daily_range_mismatch', 'date': row['date'][:10]}
                        all_verified = False; break
                    converted.append({**row, **{k: row[k] * f["a"] + f["b"] for k in PRICE_FIELDS}})
                if all_verified:
                    raw["rows"] = converted
            if all_verified:
                raw.update(adjustment="pit", adjustment_verified=True, adjustment_asof=ev["observed_at"],
                           adjustment_evidence={k: v for k, v in ev.items() if k != "days"})
                raw["adjustment_evidence"]["verified_dates"] = sorted(dates)
            else:
                raw["input_issue"] = "price_basis_cross_check_failed:" + raw.get('adjustment_details', {}).get('reason', 'unknown')
        raw["observed_at"] = now().isoformat()
    with _lock:
        for old in list(_cache):
            if old[:2] == key[:2]:
                _cache.pop(old, None)
        _cache[key] = copy.deepcopy(raw)
    return normalize(raw, timeframe, requested or now())


def disclosure(code, asof):
    if not db.q1("SELECT 1 FROM sqlite_master WHERE type='table' AND name='research_disclosures'"):
        return None
    row = db.q1('SELECT * FROM research_disclosures WHERE code=? AND available_at<=? ORDER BY period_end DESC,published_at DESC LIMIT 1', (code, stamp(asof).isoformat()))
    if row:
        return {"available_at": row["available_at"], "source": row["source"], "values": db.loads(row["values_json"], {})}
    return None


def _quote_time(row):
    # A receive clock is never substituted for a source quote clock.
    return stamp(row.get("source_time") or row.get("time"))


def _qualified_snapshot(t):
    with market._cache_lock:
        entry = market._cache.get("snapshot")
    if not entry:
        return [], None
    received, rows = entry
    known = datetime.fromtimestamp(received, CN_TZ)
    if known > t or (t - known).total_seconds() > 180:
        return [], None
    qualified = [r for r in rows if _quote_time(r) and 0 <= (t - _quote_time(r)).total_seconds() <= 180
                 and finite(r.get("pct")) and r.get("code")]
    return qualified, known


def _flow(code):
    key = ("flow", code, now().date().isoformat())
    with _lock:
        cached = _context_cache.get(key)
    if cached:
        return copy.deepcopy(cached)
    # Fetch a documented existing source directly to retain which parser won;
    # market.fundflow() currently drops provenance when caching its rows.
    secid = ("1." if code.startswith("sh") else "0.") + code[2:]
    url = ("https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get?lmt=20&klt=101&secid=" + secid
           + "&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56,f57")
    rows, source = [], "Eastmoney daily fundflow"
    try:
        payload = json.loads(market.http_get(url, "https://data.eastmoney.com/", timeout=8, retries=1))
        for item in (payload.get("data") or {}).get("klines", []):
            p = item.split(",")
            rows.append({"date": p[0], "main": float(p[1]), "main_pct": float(p[6])})
    except (RuntimeError, ValueError, TypeError, IndexError):
        pass
    if not rows:
        source = "Sina daily fundflow"
        url = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/MoneyFlow.ssl_qsfx_zjlrqs?page=1&num=20&sort=opendate&asc=0&daima=" + code
        for item in json.loads(market.http_get(url, "https://vip.stock.finance.sina.com.cn/", timeout=8, retries=1)) or []:
            if item.get("netamount") is not None:
                rows.append({"date": item["opendate"], "main": float(item["netamount"]), "main_pct": None})
    observed = now()
    rows = sorted([r for r in rows if r["date"] < observed.date().isoformat() and finite(r["main"])], key=lambda r: r["date"])
    if not rows:
        return None
    recent = rows[-5:]
    values = {"main_last_yuan": recent[-1]["main"], "main_observed_sum_yuan": sum(r["main"] for r in recent),
              "observed_days": len(recent)}
    if len(recent) == 5:
        values['main_sum5_yuan'] = values['main_observed_sum_yuan']
    if finite(recent[-1].get("main_pct")):
        values["main_pct"] = recent[-1]["main_pct"]
    result = {"source": source, "source_time": recent[-1]["date"], "source_precision": "date",
              "granularity": "day", "known_at": observed.isoformat(), "values": values}
    with _lock:
        _context_cache[key] = result
    return copy.deepcopy(result)


def context(asof, code=None):
    """Observed optional families. Missing families carry explicit coverage causes."""
    t = stamp(asof)
    if not t:
        raise ValueError("invalid context asof")
    out, missing = {}, {}
    rows, known = _qualified_snapshot(t)
    if rows:
        breadth = market.breadth(rows)
        values = {k: breadth[k] for k in ('up', 'down', 'flat', 'median_pct', 'total') if finite(breadth.get(k))}
        values['up_ratio'] = sum(r['pct'] > 0 for r in rows) / len(rows)
        if all(finite(r.get('amount')) for r in rows):
            values['amount_yi'] = breadth['amount_yi']
        if all(finite(r.get('main_net')) for r in rows):
            values['main_net_yi'] = breadth['main_net_yi']
        if all(finite(r.get('price')) and finite(r.get('prev')) and r['prev'] > 0 for r in rows):
            values.update(limit_up=breadth['limit_up'], limit_down=breadth['limit_down'])
        values["observed_symbols"] = len(rows)
        out["market"] = {"source": "source-timestamped platform snapshot subset", "source_time": min(_quote_time(r) for r in rows).isoformat(),
                         "known_at": known.isoformat(), "values": values, "coverage": "observed_subset"}
    else:
        missing["market"] = "no_fresh_source_timestamped_snapshot"
    try:
        quotes = market.quotes_recent(["sh000001", "sh000300", "sz399001"])
        observed = now()
        usable = [q for q in quotes.values() if q.get("source") and _quote_time(q) and 0 <= (observed - _quote_time(q)).total_seconds() <= 180]
        if usable:
            prior = out.get("market")
            values = dict((prior or {}).get("values", {}))
            for q in usable:
                if finite(q.get("pct")):
                    values["index_" + q["code"] + "_pct"] = q["pct"]
            if values:
                out["market"] = {"source": "source-timestamped index quotes and measured breadth",
                                 "source_time": min([_quote_time(q) for q in usable] + ([stamp(prior["source_time"])] if prior else [])).isoformat(),
                                 "known_at": observed.isoformat(), "values": values,
                                 "coverage": "indices_and_subset" if prior else "indices_only"}
                missing.pop("market", None)
    except (RuntimeError, ValueError, KeyError, TypeError):
        pass
    if code:
        code = market.norm_code(code)
        try:
            flow = _flow(code)
            if flow:
                out["flow"] = flow
            else:
                missing["flow"] = "no_completed_daily_fundflow"
        except (RuntimeError, ValueError, KeyError, TypeError):
            missing["flow"] = "public_flow_source_unavailable"
        if rows and db.q1("SELECT 1 FROM sqlite_master WHERE type='table' AND name='stock_sector'"):
            maps = db.q("SELECT code,ind_code,updated FROM stock_sector WHERE updated<=?", (t.date().isoformat(),))
            mapping = {r["code"]: r["ind_code"] for r in maps}
            industry = mapping.get(code)
            group = [r for r in rows if industry and mapping.get(r["code"]) == industry]
            if len(group) >= 3:
                values = {"mean_pct": statistics.mean(r["pct"] for r in group),
                          "up_ratio": sum(r["pct"] > 0 for r in group) / len(group), "observed_symbols": len(group),
                          "relative_market_pct": statistics.mean(r["pct"] for r in group) - statistics.mean(r["pct"] for r in rows)}
                out["sector"] = {"source": "source-timestamped member quotes plus stored industry map",
                                 "source_time": min(_quote_time(r) for r in group).isoformat(),
                                 "known_at": now().isoformat(), "values": values, "coverage": "observed_subset"}
        if "sector" not in out:
            missing["sector"] = "no_qualified_industry_members"
        if db.q1("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ann_seen'"):
            events = db.q("SELECT title,ts,tag FROM ann_seen WHERE code=? ORDER BY ts DESC LIMIT 100", (code,))
            events = [e for e in events if len(str(e["ts"])) >= 19 and stamp(e["ts"]) and 0 <= (t - stamp(e["ts"])).total_seconds() <= 86400]
            if events:
                out["event"] = {"source": "Eastmoney stored observed announcements", "source_time": max(stamp(e["ts"]) for e in events).isoformat(),
                                "known_at": now().isoformat(), "coverage": "observed_subset", "values": {"observed_count24h": len(events),
                                "risk_title_count24h": sum(any(w in e["title"] for w in ("减持", "处罚", "立案", "亏损", "风险")) for e in events)}}
        if "event" not in out:
            missing["event"] = "no_timestamped_observed_announcements"
    else:
        missing.update(flow="stock_code_required", sector="stock_code_required", event="stock_code_required")
    out["_coverage"] = {family: {"status": "observed" if family in out else "unavailable", "reason": missing.get(family),
                                   "coverage": out.get(family, {}).get("coverage", "available_source_only")}
                        for family in ("flow", "market", "sector", "event")}
    return out
