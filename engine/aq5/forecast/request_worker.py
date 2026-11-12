"""One-shot cloud analysis worker. Isolated storage; never starts the trading engine."""
from __future__ import annotations

import base64
import gzip
import hashlib
import hmac
import io
import json
import os
import re
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
    blob = cloud.read_remote_state()
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


def _encrypt(result, password, limit=512*1024):
    from .. import vault
    plain = json.dumps(result, ensure_ascii=False, allow_nan=False,
                       separators=(',', ':')).encode()
    cipher = vault.seal_web(gzip.compress(plain), password)
    if len(cipher) > limit:
        raise ValueError('analysis result exceeds queue limit')
    return {'data_b64': base64.b64encode(cipher).decode(), 'sha256': hashlib.sha256(cipher).hexdigest()}


def _decrypt(envelope, password, limit=512*1024):
    from .. import vault
    raw = base64.b64decode(envelope['data_b64'], validate=True)
    if len(raw) > limit or not raw.startswith(b'AQ5w1') or not hmac.compare_digest(
            hashlib.sha256(raw).hexdigest(), envelope.get('sha256', '')):
        raise ValueError('research ciphertext integrity failure')
    with gzip.GzipFile(fileobj=io.BytesIO(vault.unseal_web(raw, password))) as stream:
        plain = stream.read(8*1024*1024+1)
    if len(plain) > 8*1024*1024:
        raise ValueError('research plaintext exceeds limit')
    result = json.loads(plain)
    if not isinstance(result, dict):
        raise ValueError('research result must be an object')
    return result


def _restore_targets(code, envelopes, password):
    """The Durable Object owns research target heads; only this ephemeral table is replaced."""
    from .. import db, decision, decision_evidence as evidence
    if not isinstance(envelopes, list) or len(envelopes) > 2:
        raise ValueError('target head collection invalid')
    records, routes = [], set()
    for envelope in envelopes:
        record = _decrypt(envelope, password, 48*1024)
        route = record.get('route')
        if (record.get('code') != code or envelope.get('code') != code or route not in ('value','emotion')
                or route != envelope.get('route') or route in routes or record.get('id') != envelope.get('id')):
            raise ValueError('target identity mismatch')
        evidence.stamp(record['created_at']); evidence.stamp(record['confirmed_at'])
        routes.add(route); records.append(record)
    decision.init()
    with db.tx() as conn:
        # This function is called only after require_isolated_mode in the worker entry path.
        conn.execute('DELETE FROM decision_targets WHERE code=?', (code,))
        for record in records:
            conn.execute('INSERT INTO decision_targets VALUES(?,?,?,?,?,?,?)',
                         (record['id'],code,record['route'],record['created_at'],record['confirmed_at'],
                          None,db.dumps({k:v for k,v in record.items() if k!='confirmed_at'})))


def fulfill(item, password, analyze, *, context=None, handlers=None):
    kind = item.get('kind', 'forecast')
    target_update = None
    if kind == 'forecast':
        result = analyze(item['code'])
        if (result.get('symbol') or {}).get('code') != item['code']:
            raise ValueError('analysis symbol mismatch')
    elif kind in ('analysis','screen','confirm'):
        require_isolated_mode()
        if not isinstance(context, dict):
            raise ValueError('research context missing')
        if kind != 'screen':
            _restore_targets(item['code'], context.get('targets', []), password)
        if handlers is None:
            from .. import decision_collect
            handlers = {'analysis': decision_collect.analysis, 'screen': decision_collect.screen}
        if kind == 'analysis':
            result = handlers['analysis'](item['code'])
            if result.get('code') != item['code'] or result.get('fusion', {}).get('code') != item['code']:
                raise ValueError('analysis result identity mismatch')
        elif kind == 'screen':
            result = handlers['screen'](read_only=True) if item.get('read_only') is True else handlers['screen']()
            if result.get('kind') not in ('research_preview','screen') or not isinstance(result.get('routes'), dict):
                raise ValueError('screen result contract mismatch')
            result = {**result, 'kind':'screen'}
        else:
            from .. import decision
            source = context.get('source') or {}
            if (source.get('id') != item.get('source_request_id') or source.get('code') != item['code']
                    or source.get('kind') != 'analysis'):
                raise ValueError('confirmation source identity mismatch')
            original = _decrypt(source, password)
            route = item.get('route')
            if route not in ('value','emotion') or original.get('code') != item['code']:
                raise ValueError('confirmation route mismatch')
            candidate = original.get('fusion', {}).get('routes', {}).get(route, {}).get('proposal')
            if not isinstance(candidate, dict) or candidate.get('id') != item.get('proposal_id'):
                raise ValueError('confirmation proposal mismatch')
            # Collect current evidence before decision.confirm_target recomputes the current structure.
            fresh = handlers['analysis'](item['code'])
            if fresh.get('code') != item['code']:
                raise ValueError('fresh analysis identity mismatch')
            decision.remember({'routes': {route: {'proposal': candidate}}})
            saved = decision.confirm_target({'code':item['code'],'route':route,
                                             'proposal_id':candidate['id'],'previous_id':item.get('previous_id')})
            target_update = {'code':item['code'],'route':route,'id':saved['id'],
                             'previous_id':item.get('previous_id'), **_encrypt(saved,password,48*1024)}
            result = {'kind':'confirm','code':item['code'],'route':route,'confirmed_target':saved}
    else:
        raise ValueError('unsupported cloud request kind')
    return {'id': item['id'], 'claim': item['claim'],
            **_encrypt(result,password), **({'target_update':target_update} if target_update else {})}


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


def run(*, budget=900, transport=exchange, analyze=None, clock=time.monotonic, wait=time.sleep, research=False,
        model_loader=None, request_id=''):
    require_isolated_mode()
    password = os.environ.get('AQ5_KEY', '')
    relay = os.environ.get('AQ5_RELAY', '')
    if not password or not relay:
        raise RuntimeError('cloud analysis credentials unavailable')
    # The caller MUST set AQ5_DATA to a dedicated, temporary job directory.
    if analyze is None:
        from .service import analyze
    key = hmac.new(password.encode(), b'aq5-live-publish-v1', hashlib.sha256).hexdigest()
    poll_action = 'poll?protocol=research-v1' if research else 'poll'
    if request_id:
        if not re.fullmatch(r'[a-f0-9]{32}', request_id):
            raise ValueError('invalid cloud request ID')
        poll_action += ('&' if research else '?') + 'id=' + request_id
    max_requests = 1 if request_id else 20
    deadline, count, delivery_failures = clock() + budget, 0, 0
    models_loaded=False
    while clock() < deadline and count < max_requests:
        jobs = transport(relay, key, poll_action).get('requests', [])
        if not jobs:
            break
        if request_id and (len(jobs) != 1 or jobs[0].get('id') != request_id):
            raise RuntimeError('cloud relay did not honor the requested job ID')
        for item in jobs:
            try:
                if item.get('kind','forecast')=='forecast' and model_loader is not None and not models_loaded:
                    try:model_loader()
                    except Exception:print('Model snapshot unavailable; probabilities remain unverified.',flush=True)
                    models_loaded=True
                context = transport(relay, key, 'context?id='+item['id']) if item.get('kind','forecast')!='forecast' else None
                payload = fulfill(item, password, analyze, context=context)
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
    run(research=True, model_loader=lambda:load_models(os.environ.get('AQ5_KEY','')),
        request_id=os.environ.get('AQ5_FORECAST_REQUEST_ID',''))


if __name__ == '__main__':
    main()
