"""Replay observed paper fills from a pinned interrupted cloud checkpoint."""
from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from datetime import datetime
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aq5 import cloud, db, market, sim, vault
from aq5.config import CN_TZ, DATA, DEFAULTS, _merge, load_settings, now

STATE = '967591686b0b0daeda0ae4379f4358c7bdb000fb'
DISPLAY = 'cc9f3fa65e0afd8b94de8a168b0cb4f0864ebd69'
CUTOFF = '2026-11-11 11:05:59'
BASE_HASH = 'c5eec304d3ceffce971d39cbf8edfdd3bbb5665cf3c1ad52359d5c8fb049cb28'
VIEW_HASH = '6668091795a00481f17e77afc82051f374a8bfc2f3a3e8d3b48496785b053c45'
OVERVIEW_HASH = '544ff75e667db42fe82729ee4faed9ae7cee5e106990eaee0f0bd9a531a12af4'
REPO = 'Suqi854/-A'
RECOVERY_BRANCH = 'recovery/aq5-sim-20261111'
BACKUP_BRANCH = 'backup/aq5-state-20261111-110559'
FILL = re.compile(r'^AI自动盘 (买入|卖出|平空|融券卖出) (.+) (\d+)股 @ (\d+\.\d{2})(?:，盈亏 ([+-]\d+\.\d{2}))?$')


def checked(blob, digest):
    if not blob or hashlib.sha256(blob).hexdigest() != digest:
        raise ValueError('Recovery input digest mismatch')
    return blob


def reconcile_account(public_ai):
    actual = {p['code']:p for p in db.q("SELECT * FROM positions WHERE account='ai'")}
    expected = {p['code']:p for p in public_ai['positions']}
    cash = db.q1("SELECT cash FROM accounts WHERE id='ai'")['cash']
    if round(cash,2) != public_ai['cash'] or {k:p['qty'] for k,p in actual.items()} != {k:p['qty'] for k,p in expected.items()}:
        raise ValueError('AI cash/positions do not reconcile with later snapshot')
    for code, position in actual.items():
        saved = expected[code]
        price = saved['price']
        for column, field in (('stop','to_stop'),('target1','to_target')):
            original = position[column]
            ratio = round((original / price - 1) * 100, 2) if original else None
            if ratio != saved[field]:
                raise ValueError('AI position risk level not reconciled')
        if round((price-position['cost'])*position['qty'],2) != saved['pnl']:
            raise ValueError('AI position cost not reconciled')


def recover(con, settings, private, overview):
    base = {r['account']: r['count'] for r in db.q('SELECT account, COUNT(*) count FROM trades GROUP BY account')}
    if 'ai' not in base or not settings['strategy_v2']['legacy_auto']:
        raise ValueError('Unexpected source account configuration')
    if db.q1("SELECT 1 FROM forecast_controls WHERE name='active_version' AND value NOT IN ('0','null')"):
        raise ValueError('Unexpected forecasting strategy ownership')
    public_ai = overview['accounts']['ai']
    names = {}
    for row in db.q('SELECT code,name FROM positions UNION SELECT code,name FROM trades UNION SELECT code,name FROM universe') + public_ai['positions']:
        names.setdefault(row['name'], set()).add(market.norm_code(row['code']))
    events = {}
    for event in overview['events'] + private['events']:
        if event['time'] <= CUTOFF or event['kind'] != 'trade' or not event['title'].startswith('AI自动盘'):
            continue
        if event['id'] in events and events[event['id']] != event:
            raise ValueError('Conflicting evidence for the same event')
        events[event['id']] = event
    if len(events) != 21:
        raise ValueError('Unexpected number of recoverable AI fills')
    details = []
    checkpoint_count = None
    with patch.object(sim, 'load_settings', return_value=settings), patch.object(db, 'event'):
        for event in sorted(events.values(), key=lambda row: (row['time'], row['id'])):
            match = FILL.fullmatch(event['title'])
            if not match or event['time'] > private['time']:
                raise ValueError('Fill evidence outside the preserved snapshot')
            if event['time'] > overview['time'] and checkpoint_count is None:
                reconcile_account(public_ai)
                checkpoint_count = len(details)
            direction, name, quantity, price, pnl = match.groups()
            quantity, price = int(quantity), float(price)
            side = 'buy' if direction in ('买入', '平空') else 'sell'
            positions = {p['code']:p for p in db.q("SELECT * FROM positions WHERE account='ai'")}
            candidates = []
            for code in names.get(name, ()):
                position = positions.get(code)
                old = position['qty'] if position else 0
                observed = ('平空' if old < 0 else '买入') if side == 'buy' else ('卖出' if old > 0 else '融券卖出')
                if observed == direction:
                    candidates.append(code)
            if len(candidates) != 1:
                raise ValueError('Stock identity unresolved at event '+str(event['id']))
            code = candidates[0]
            reason = event['body']
            pending = db.q("SELECT * FROM orders WHERE account='ai' AND status='pending' AND code=? AND side=? AND qty=?",
                           (code, side, quantity))
            if len(pending) > 1 or pending and pending[0]['reason'] != reason:
                raise ValueError('Existing order ambiguity at event '+str(event['id']))
            if pending:
                order = pending[0]
            else:
                with db.tx() as conn:
                    oid = conn.execute(
                        """INSERT INTO orders(account,code,name,side,kind,qty,status,created,updated,reason,note,auto)
                        VALUES('ai',?,?,?,'market',?,'pending',?,?,?,'由原成交事件恢复，时间取事件时间',1)""",
                        (code, name, side, quantity, event['time'], event['time'], reason)).lastrowid
                order = db.q1('SELECT * FROM orders WHERE id=?',(oid,))
            moment = datetime.fromisoformat(event['time']).replace(tzinfo=CN_TZ)
            with patch.object(sim, 'now', return_value=moment), patch.object(sim, 'today', return_value=event['time'][:10]):
                sim._execute_ai(order, price)
            fill = db.q1('SELECT * FROM trades WHERE order_id=?',(order['id'],))
            if not fill or fill['pnl'] != (float(pnl) if pnl is not None else None):
                raise ValueError('Realized P&L mismatch at event '+str(event['id']))
            if '第一止盈' in reason:
                position = db.q1("SELECT * FROM positions WHERE account='ai' AND code=?",(code,))
                if position:
                    with db.tx() as conn:
                        conn.execute("UPDATE positions SET stop=?,stage=1 WHERE account='ai' AND code=?",
                                     (round(max(position['cost'],position['stop'] or 0),2),code))
            with db.tx() as conn:
                conn.execute('INSERT INTO events(id,time,level,kind,title,body,read) VALUES(?,?,?,?,?,?,?)',
                             tuple(event[k] for k in ('id','time','level','kind','title','body','read')))
            # High-frequency orders carry no attached exits. Other observed
            # closures are accepted only if they did not establish exposure.
            before = positions.get(code,{}).get('qty',0)
            closing_only = before and (before > 0) != (side == 'buy') and quantity <= abs(before)
            t_buyback = before > 0 and side == 'buy' and reason.startswith('做T低吸买回：')
            if not pending and not closing_only and not t_buyback and not reason.startswith('高频'):
                raise ValueError('Unverified original strategy entry terms at event '+str(event['id']))
            details.append({'event':event['id'],'order':order['id'],'trade':fill['id'],'event_time':event['time']})
    if checkpoint_count is None:
        reconcile_account(public_ai)
        checkpoint_count = len(details)
    remaining = db.q1("SELECT COUNT(*) n FROM orders WHERE account='ai' AND status='pending'")['n']
    if remaining:
        raise ValueError('Unreconciled AI orders remain pending')
    forecast = private['forecast']
    comparisons = forecast['ledger']['books']
    latest_orders = [
        {'book':book,'order':order,'cash':data['cash'],'holdings':data['holdings'],'cycle':next((c for c in data['open_cycles'] if
             (c['code'],c['side'],c['opened_at']) == (order['code'],order['side'],order['fill_time'])),None)}
        for book,data in comparisons.items() for order in data['orders']
        if not db.q1('SELECT 1 FROM forecast_orders WHERE id=?',(order['id'],))
    ]
    if len(latest_orders) != 1 or not latest_orders[0]['cycle']:
        raise ValueError('Unexpected forecast comparison evidence')
    audit = {'source_state':STATE,'display_snapshot':DISPLAY,'through':details[-1]['event_time'],'balance_checked_at':overview['time'],
             'restored_ai_fills':details,'comparison_fills_evidence':latest_orders,
             'prediction_evidence':forecast['predictions'],
             'note':'恢复的成交时间取原成交事件时间。对照盘证据单独保留，不计为完整原始订单。'}
    with db.tx() as conn:
        conn.execute('INSERT INTO forecast_ledger_events VALUES(?,?,?,?)',
                     ('recovery-'+STATE,now().isoformat(),'cloud_recovery',db.dumps(audit)))
        conn.execute("INSERT INTO forecast_controls(name,value) VALUES('paused','true') ON CONFLICT(name) DO UPDATE SET value='true'")
        conn.execute('INSERT INTO events(time,level,kind,title,body) VALUES(?,?,?,?,?)',
                     (now().strftime('%Y-%m-%d %H:%M:%S'),'info','system','云端模拟盘账本已恢复',
                      '核对至11月11日14:04。恢复21笔AI模拟成交。对照盘恢复证据随加密账本保存。'))
    assert con.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
    counts = {r['account']:r['count'] for r in db.q('SELECT account, COUNT(*) count FROM trades GROUP BY account')}
    if counts != {**base,'ai':base['ai']+len(details)}:
        raise ValueError('Unexpected recovered trade counts')
    return {'ai_trades':counts['ai'],'all_original_trades_preserved':True,'recovered_ai_fills':len(details),'comparison_evidence':len(latest_orders),
            'prediction_evidence':len(forecast['predictions']),'cash_positions_cost_risk_match':True,
            'snapshot_time':overview['time'],'fills_at_balance_checkpoint':checkpoint_count,
            'last_event_time':details[-1]['event_time'],'pending_ai_orders':remaining}


def recovery_guard():
    if (not cloud.CLOUD or os.environ.get('GITHUB_REPOSITORY') != REPO
            or os.environ.get('GITHUB_REF_NAME') != RECOVERY_BRANCH or now().hour < 16):
        raise ValueError('Recovery publish is restricted to the authorized cloud branch after hours')
    meta = cloud.remote_meta()
    if not meta or meta['_state_commit'] != STATE or meta.get('active') is not True or meta.get('updated') != CUTOFF:
        raise ValueError('The original state ref changed or was already recovered')
    backup_url = f'https://api.github.com/repos/{REPO}/git/ref/heads/{BACKUP_BRANCH}'
    backup = json.loads(cloud._http(backup_url,timeout=25))
    if backup['object']['sha'] != STATE:
        raise ValueError('Original encrypted checkpoint was not pinned')
    relay = cloud._cfg()['relay']
    if relay != 'https://aq5-relay.phehwm62784.workers.dev':
        raise ValueError('Unexpected relay')
    if json.loads(cloud._http(relay+'/__aq5/alive',timeout=25)).get('alive') is not False:
        raise ValueError('The local engine may be online')


def publish(payload, result, connection, password):
    expected_tables = len(cloud._tables(connection))
    payload['db'] = base64.b64encode(gzip.compress(connection.serialize(),6)).decode()
    connection.close()
    del db._local.c
    blob = vault.seal(json.dumps(payload).encode(), password)
    del payload['db']

    # Use the same import path and schema initialization as regular cloud duty.
    from aq5 import ai, announce, auth, marketstate, sectors, signals
    db.init()
    for module in (auth, ai, sectors, signals, marketstate, announce):
        module.init()
    imported = cloud.unpack(blob,take_settings=True)
    if imported['tables'] != expected_tables or not load_settings()['strategy_v2']['legacy_auto']:
        raise ValueError('Normal cloud startup import did not preserve tables/configuration')
    if db.q1("SELECT COUNT(*) n FROM trades WHERE account='ai'")['n'] != result['ai_trades']:
        raise ValueError('Normal cloud import did not preserve AI fills')
    if not db.q1("SELECT 1 FROM forecast_ledger_events WHERE id=?",('recovery-'+STATE,)):
        raise ValueError('Normal cloud import did not preserve recovery evidence')
    if db.q1('PRAGMA quick_check')['quick_check'] != 'ok':
        raise ValueError('Imported database failed its integrity check')
    db.freeze_writes()

    root = DATA/'state_repo'
    root.mkdir()
    cloud._write_state_chunks(root, blob)
    stamp = now().strftime('%Y-%m-%d %H:%M:%S')
    meta = {'owner':'cloud','active':False,'updated':stamp,'heartbeat':stamp,
            'state_format':'AQ5v1-chunks','state_manifest':cloud.STATE_MANIFEST,
            'recovery':{'base_state':STATE,'verified_through':result['last_event_time']}}
    (root/'cloud.json').write_text(json.dumps(meta),encoding='utf-8')
    env = dict(os.environ)
    authorization = base64.b64encode(('x-access-token:'+env['GITHUB_TOKEN']).encode()).decode()
    env.update(GIT_CONFIG_COUNT='1',GIT_CONFIG_KEY_0='http.https://github.com/.extraheader',
               GIT_CONFIG_VALUE_0='AUTHORIZATION: basic '+authorization,GIT_TERMINAL_PROMPT='0')

    def git(*args, timeout=30):
        proc = subprocess.run(['git',*args],cwd=root,env=env,capture_output=True,text=True,timeout=timeout)
        if proc.returncode:
            detail = (proc.stderr or proc.stdout).replace(authorization,'[redacted]')
            raise RuntimeError(cloud._safe_git_error(detail))
        return proc.stdout.strip()

    git('init','-q')
    git('config','user.name','aq5-cloud-recovery')
    git('config','user.email','aq5-cloud@users.noreply.github.com')
    git('add','--','cloud.json',cloud.STATE_MANIFEST,*sorted(p.name for p in root.glob('state-part-*.bin')))
    git('commit','-qm','Restore reconciled encrypted AI simulation checkpoint')
    commit = git('rev-parse','HEAD')
    recovery_guard()
    print(json.dumps({'prepared':commit,'tables_imported':imported['tables'],'encrypted_sha256':hashlib.sha256(blob).hexdigest()}),flush=True)
    git('push',f'--force-with-lease=refs/heads/state:{STATE}',f'https://github.com/{REPO}.git',
        'HEAD:refs/heads/state',timeout=300)
    if cloud._state_ref() != commit or cloud.read_remote_state(ref=commit) != blob:
        raise ValueError('Published recovery checkpoint could not be confirmed')
    print(json.dumps({'published':commit,'verified':True,'ai_trades':result['ai_trades']}),flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--local-evidence', type=Path)
    parser.add_argument('--publish', action='store_true')
    args = parser.parse_args()
    if args.publish:
        if args.local_evidence:
            raise ValueError('Local rehearsal cannot publish')
        recovery_guard()
    prefix = f'https://raw.githubusercontent.com/Suqi854/-A/{DISPLAY}/data/'
    overview = json.loads(checked(cloud._http(prefix+'latest.json',timeout=60), OVERVIEW_HASH))
    if args.local_evidence:
        root = args.local_evidence
        vault.VAULT = Path(os.environ['AQ5_RECOVERY_VAULT'])
        password = vault.get('mobile_password')
        blob = (root/f'state-size-{STATE}.enc').read_bytes()
        view = (root/'recovery-evidence'/f'private-{DISPLAY}.enc').read_bytes()
    else:
        password = os.environ.get('AQ5_KEY')
        blob = cloud.read_remote_state(ref=STATE)
        view = cloud._http(prefix+'private.enc',timeout=60)
    if not password:
        raise ValueError('Recovery credential unavailable')
    payload = json.loads(vault.unseal(checked(blob,BASE_HASH),password))
    del blob
    private = json.loads(gzip.decompress(vault.unseal_web(checked(view,VIEW_HASH),password)))
    raw = bytearray(gzip.decompress(base64.b64decode(payload['db'])))
    raw[18:20] = b'\x01\x01'
    connection = sqlite3.connect(':memory:')
    connection.deserialize(raw)
    del raw
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA temp_store=MEMORY')
    db._local.c = connection
    result = recover(connection,_merge(DEFAULTS,payload['settings']),private,overview)
    print(json.dumps(result,ensure_ascii=True),flush=True)
    if args.publish:
        publish(payload,result,connection,password)


if __name__ == '__main__':
    main()
