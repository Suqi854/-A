"""One-shot cloud analysis worker. Isolated storage; never starts the trading engine."""
from __future__ import annotations

import base64
import gzip
import hashlib
import hmac
import json
import os
import sqlite3
import time
import urllib.error
import urllib.request


def require_isolated_mode():
    # Validate BEFORE importing config/db/vault, whose imports can create DATA.
    if not os.environ.get('AQ5_DATA') or os.environ.get('AQ5_FORECAST_REQUEST') != '1':
        raise RuntimeError('isolated request worker required')


def exchange(relay, key, action, body=None):
    headers = {'User-Agent': 'aq5-forecast-request', 'x-aq5-live-key': key}
    data = None
    if body is not None:
        data = json.dumps(body, separators=(',', ':')).encode()
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(relay.rstrip('/') + '/__aq5/forecast/' + action,
                                 data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=45) as response:
        return json.load(response)


def load_models(password):
    """Import ONLY JSON model rows into this ephemeral database, never account state."""
    from .. import cloud, db, vault
    from . import models
    models.init()
    blob = cloud._gh_raw('state.bin')
    if not blob:
        return
    state = json.loads(vault.unseal(blob, password))
    raw = bytearray(gzip.decompress(base64.b64decode(state['db'])))
    raw[18:20] = b'\x01\x01'
    src = sqlite3.connect(':memory:')
    try:
        src.deserialize(bytes(raw))
        if not src.execute("SELECT 1 FROM sqlite_master WHERE name='forecast_models'").fetchone():
            return
        columns = ('version', 'created', 'week', 'status', 'training_hash', 'artifact', 'metrics')
        rows = src.execute('SELECT ' + ','.join(columns) +
                           " FROM forecast_models WHERE status IN ('active','candidate')").fetchall()
        with db.tx() as dst:
            dst.executemany('INSERT OR REPLACE INTO forecast_models(' + ','.join(columns) +
                            ') VALUES(?,?,?,?,?,?,?)', rows)
    finally:
        src.close()


def fulfill(item, password, analyze):
    from .. import vault
    result = analyze(item['code'])
    if (result.get('symbol') or {}).get('code') != item['code']:
        raise ValueError('analysis symbol mismatch')
    plain = json.dumps(result, ensure_ascii=False, allow_nan=False,
                       separators=(',', ':')).encode()
    cipher = vault.seal_web(gzip.compress(plain), password)
    if len(cipher) > 512 * 1024:
        raise ValueError('analysis result exceeds queue limit')
    return {'id': item['id'], 'claim': item['claim'],
            'data_b64': base64.b64encode(cipher).decode(),
            'sha256': hashlib.sha256(cipher).hexdigest()}


def complete(relay, key, payload, transport, wait):
    """Completion is idempotent; a stale claim must not abandon other jobs."""
    for attempt in range(3):
        try:
            return transport(relay, key, 'complete', payload)
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 409):
                return {'ok': False, 'expired': True}
            if exc.code not in (408, 429, 500, 502, 503, 504) or attempt == 2:
                raise
        except (OSError, ValueError):
            if attempt == 2:
                raise
        wait(2 ** attempt)


def run(*, budget=900, transport=exchange, analyze=None, clock=time.monotonic, wait=time.sleep):
    require_isolated_mode()
    password = os.environ.get('AQ5_KEY', '')
    relay = os.environ.get('AQ5_RELAY', '')
    if not password or not relay:
        raise RuntimeError('cloud analysis credentials unavailable')
    # The caller MUST set AQ5_DATA to a dedicated, temporary job directory.
    if analyze is None:
        from .service import analyze
    key = hmac.new(password.encode(), b'aq5-live-publish-v1', hashlib.sha256).hexdigest()
    deadline, count, delivery_failures = clock() + budget, 0, 0
    while clock() < deadline and count < 20:
        jobs = transport(relay, key, 'poll').get('requests', [])
        if not jobs:
            break
        for item in jobs:
            try:
                payload = fulfill(item, password, analyze)
            except Exception:
                # Logs and the queue never contain credentials, holdings, or provider bodies.
                payload = {'id': item['id'], 'claim': item['claim'], 'error': True}
            try:
                complete(relay, key, payload, transport, wait)
            except (OSError, ValueError):
                delivery_failures += 1
                print('Cloud analysis result delivery unavailable.', flush=True)
            count += 1
    if delivery_failures:
        raise RuntimeError('cloud result delivery incomplete')
    print('Cloud analysis requests completed:', count, flush=True)
    return count


def main():
    require_isolated_mode()
    from .. import db
    db.init()
    try:
        load_models(os.environ.get('AQ5_KEY', ''))
    except Exception:
        # Structural analysis is useful without a trained probability bundle.
        print('Model snapshot unavailable; probabilities remain unverified.', flush=True)
    run()


if __name__ == '__main__':
    main()
