"""Bounded live sampling and encrypted relay publication; no broker or legacy orders."""
from __future__ import annotations
import base64
import gzip
import hashlib
import hmac
import json
import os
import threading
import time
import urllib.request
import uuid
from . import db, market, vault
from .config import MOCK, load_settings, now

VERSION = '5.2-shadow.1'
_stop = threading.Event()
_lock = threading.Lock()
_started = False
_threads = []
STATE = {'running': False, 'last_sample': None, 'last_publish': None, 'error': None, 'sample_ms': None}
_quotes = {}


def init():
    # All replicated schemas must exist BEFORE cloud.unpack.
    from . import accounting, fundamentals, intraday, research, training, forecast
    with db.tx() as conn:
        accounting.init(conn)
    for module in (fundamentals, intraday, research, training, forecast):
        module.init()


def relay_url():
    from . import cloud
    return cloud._cfg().get('relay', '').rstrip('/')


def publish_key(password):
    return hmac.new(password.encode(), b'aq5-live-publish-v1', hashlib.sha256).hexdigest()


def codes():
    from . import picker, plan, training
    s = load_settings()
    ordered = training.codes() + [r['code'] for r in db.q('SELECT DISTINCT code FROM positions')]
    if db.q1("SELECT 1 FROM sqlite_master WHERE type='table' AND name='forecast_books'"):
        for row in db.q('SELECT holdings,baseline FROM forecast_books'):
            ordered += list(db.loads(row['holdings'],{}))
            ordered += list(db.loads(row['baseline'],{}).get('quantities',{}))
    ordered += s.get('watchlist', [])
    recent = picker.runs(1)
    if recent:
        ordered += [p['code'] for p in recent[0].get('items', [])]
    return list(dict.fromkeys(market.norm_code(c) for c in ordered if c))[:40]



def payload():
    from . import intraday, training
    return {'signals': intraday.latest(), 'status': {**intraday.status(), 'transport': dict(STATE)},
            'training': training.view()}


def private_snapshot():
    from . import sim, plan, intraday, training, forecast
    return {'time': now().strftime('%Y-%m-%d %H:%M:%S'), 'me': sim.view('me'), 'plan': plan.view(),
            'intraday': intraday.latest(), 'intraday_status': {**intraday.status(), 'transport': dict(STATE)},
            'training': training.view(), 'forecast': forecast.view(limit=36,compact=True),
            'look': load_settings().get('look') or {}}


def public_snapshot(quotes=None):
    from . import picker
    qs = quotes if quotes is not None else dict(_quotes)
    s = load_settings()
    recent = picker.runs(1)
    picks = recent[0].get('items', []) if recent else []
    def compact(q):
        # Keep the deployed Worker's strict public allowlist. Source evidence stays
        # in the authenticated/private stream; extra keys reject the whole packet.
        return {k: q.get(k) for k in ('code', 'name', 'price', 'pct', 'time', 'chg', 'amount')}
    pqs = {p['code']: compact(qs[p['code']]) for p in picks[:40] if p['code'] in qs}
    indices = [compact(qs[c]) for c in s['indices'][:12] if c in qs]
    times = [q.get('time') for q in list(pqs.values()) + indices if q.get('time')]
    return {'time': now().strftime('%Y-%m-%d %H:%M:%S'), 'version': VERSION, 'phase': market.market_phase(),
            'pick_quotes': pqs, 'indices': indices, 'quote_time': min(times) if times else None}


def publish_once(stream_id, sequence, *, url=None, password=None, channel='main', public=None, private=None):
    from . import cloud
    from .server import clean
    if MOCK and channel != 'diagnostic':
        raise ValueError('mock data cannot be published to main live channel')
    target, key = url or relay_url(), password or cloud._key()
    if not target or not key:
        return {'ok': False, 'reason': 'relay or mobile password missing'}
    private = private_snapshot() if private is None else private
    enc = vault.seal_web(gzip.compress(json.dumps(clean(private), ensure_ascii=False, separators=(',', ':')).encode()), key)
    body = {'stream_id': stream_id, 'sequence': sequence, 'role': 'diagnostic' if channel == 'diagnostic' else ('cloud' if cloud.CLOUD else 'pc'),
            'sent_at': int(time.time() * 1000), 'public': clean(public_snapshot() if public is None else public),
            'private_pack_sha256': hashlib.sha256(enc).hexdigest(), 'private_b64': base64.b64encode(enc).decode()}
    raw = json.dumps(body, separators=(',', ':')).encode()
    if len(raw) > 512 * 1024:
        raise ValueError('live package exceeds bounded size')
    req = urllib.request.Request(target.rstrip('/') + '/__aq5/live?channel=' + channel, data=raw,
                                 headers={'User-Agent': market.UA, 'Content-Type': 'application/json', 'x-aq5-live-key': publish_key(key)}, method='POST')
    with urllib.request.urlopen(req, timeout=8) as response:
        result = json.load(response)
    if result.get('ok'):
        STATE.update(last_publish=now().isoformat(), error=None)
    return result


def _sample_loop():
    from . import intraday, training
    due = 0.0
    while not _stop.is_set():
        start = time.monotonic()
        try:
            phase = market.market_phase()
            interval = 3 if phase == 'trading' else 60
            if start >= due:
                due = start + interval
                watch = codes()
                if phase == 'trading':
                    batch = intraday.runtime_tick(watch, return_quotes=True)
                    if _stop.is_set():
                        break
                    with _lock:
                        _quotes.update(batch['quotes'])
                    from .forecast import runtime as forecasting
                    forecasting.feed(batch['quotes'])
                    training.tick(batch['signals'], batch['quotes'], asof=batch['asof'])
                else:
                    training.tick({}, {}, asof=now())
                    # A previous close may value a portfolio, but cannot trigger a new intraday signal.
                    current = market.quotes(watch)
                    with _lock:
                        _quotes.update(current)
                from . import picker
                recent = picker.runs(1)
                public_codes = [p['code'] for p in recent[0].get('items', [])] if recent else []
                indices = market.quotes_recent(list(dict.fromkeys(public_codes + load_settings()['indices'])))
                with _lock:
                    _quotes.update(indices)
                STATE.update(last_sample=now().isoformat(), sample_ms=round((time.monotonic()-start)*1000))
        except Exception as exc:
            STATE['error'] = type(exc).__name__  # never log URLs, tokens or private ledger values
        _stop.wait(max(.1, 3-(time.monotonic()-start)))


def _publish_loop():
    stream, seq = uuid.uuid4().hex, 0
    while not _stop.is_set():
        try:
            if relay_url() and not MOCK:
                seq += 1
                publish_once(stream, seq)
        except Exception as exc:
            STATE['error'] = type(exc).__name__
        # The transport heartbeat is independent of quote freshness. Both timestamps are exposed.
        _stop.wait(5 if market.market_phase() == 'trading' else 30)


def start():
    global _started
    with _lock:
        if _started:
            return
        _started = True
    init()
    _stop.clear()
    STATE['running'] = True
    for target, name in ((_sample_loop, 'aq5-intraday-samples'), (_publish_loop, 'aq5-encrypted-live')):
        worker = threading.Thread(target=target, name=name, daemon=True)
        _threads.append(worker)
        worker.start()
    from .forecast import runtime as forecasting
    forecasting.start()


def stop(timeout=30):
    from .forecast import runtime as forecasting
    forecasting.stop()
    _stop.set()
    deadline = time.monotonic() + timeout
    for worker in _threads:
        worker.join(max(0, deadline - time.monotonic()))
    if any(worker.is_alive() for worker in _threads):
        raise RuntimeError('live shutdown still in flight; state handoff not finalized')
    STATE['running'] = False
