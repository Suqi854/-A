import os
import sys
import tempfile
import unittest
import json
import gzip
import base64
import hashlib
import sqlite3
import urllib.error
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('AQ5_DATA', str(Path(os.environ.get('AQ5_TEST_DATA', tempfile.gettempdir())) / 'request-worker'))
from aq5.forecast import request_worker as worker
from aq5 import vault


class WorkerTest(unittest.TestCase):
    @patch.dict(os.environ, {'AQ5_KEY':'test-password','AQ5_RELAY':'https://example.test','AQ5_FORECAST_REQUEST':'1'})
    def test_research_worker_advertises_protocol_without_changing_legacy_poll(self):
        calls=[]
        def transport(relay,key,action,body=None):calls.append(action);return {'requests':[]}
        self.assertEqual(worker.run(transport=transport,analyze=lambda code:{},research=True),0)
        self.assertEqual(calls,['poll?protocol=research-v1'])

    @patch.dict(os.environ, {'AQ5_KEY':'test-password','AQ5_RELAY':'https://example.test','AQ5_FORECAST_REQUEST':'1'})
    def test_worker_only_drains_its_dispatched_request_and_skips_unneeded_models(self):
        request_id = 'a' * 32
        item = {'id': request_id, 'claim': 'b' * 32, 'kind': 'analysis', 'code': 'sh600000'}
        calls = []
        def transport(relay, key, action, body=None):
            calls.append(action)
            if action.startswith('poll'):
                return {'requests': [item]}
            return {}
        with patch.object(worker, 'fulfill', return_value={}) as fulfill:
            count = worker.run(transport=transport, analyze=lambda code: {}, research=True,
                               request_id=request_id, model_loader=lambda: calls.append('models'))
        self.assertEqual(count, 1)
        self.assertEqual(calls, ['poll?protocol=research-v1&id='+request_id, 'context?id='+request_id, 'complete'])
        fulfill.assert_called_once()

    @patch.dict(os.environ, {'AQ5_FORECAST_REQUEST':'1'})
    def test_research_analysis_restores_only_encrypted_target_heads(self):
        from aq5 import db, decision
        db.init()
        before = db.q1('SELECT COUNT(*) n FROM accounts')['n']
        target = {'id':'d'*64,'code':'sh600000','route':'emotion','created_at':'2026-11-11T10:00:00+08:00',
                  'confirmed_at':'2026-11-11T10:01:00+08:00','target_zone':[11,12],'stop':9}
        envelope = {'code':target['code'],'route':target['route'],'id':target['id'],**worker._encrypt(target,'password')}
        item = {'id':'a'*32,'claim':'b'*32,'kind':'analysis','code':'sh600000'}
        called = []
        def analyze(code):
            called.append(decision.confirmed(code,'emotion'))
            return {'code':code,'fusion':{'code':code,'routes':{}},'name':'test'}
        payload = worker.fulfill(item,'password',None,context={'targets':[envelope]},handlers={'analysis':analyze})
        self.assertEqual(called[0]['target_zone'],[11,12])
        self.assertEqual(worker._decrypt(payload,'password')['code'],'sh600000')
        self.assertNotIn('target_update',payload)
        self.assertEqual(db.q1('SELECT COUNT(*) n FROM accounts')['n'],before)
        # Durable head is authoritative even if a prior failed CAS left local staged confirmation.
        worker._restore_targets('sh600000',[],'password')
        self.assertIsNone(decision.confirmed('sh600000','emotion'))

    @patch.dict(os.environ, {'AQ5_FORECAST_REQUEST':'1'})
    def test_screen_kind_has_no_stock_binding(self):
        item = {'id':'a'*32,'claim':'b'*32,'kind':'screen','code':None}
        result = worker.fulfill(item,'password',None,context={'targets':[]},
                               handlers={'screen':lambda *, read_only: {'kind':'research_preview','routes':{'value':{},'emotion':{}}}
                                         if read_only else self.fail('legacy screen must also be read only')})
        self.assertEqual(worker._decrypt(result,'password')['kind'],'screen')
        self.assertNotIn('target_update',result)

    @patch.dict(os.environ, {'AQ5_FORECAST_REQUEST':'1'})
    def test_read_only_screen_passes_no_start_contract_to_handler(self):
        calls=[]
        def screen(*,read_only=False):
            calls.append(read_only)
            return {'kind':'research_preview','routes':{'value':{},'emotion':{}}}
        item={'id':'a'*32,'claim':'b'*32,'kind':'screen','code':None,'read_only':True,'cache_key':'c'*32}
        result=worker.fulfill(item,'password',None,context={'targets':[]},handlers={'screen':screen})
        self.assertEqual(calls,[True]);self.assertEqual(worker._decrypt(result,'password')['kind'],'screen')

    @patch.dict(os.environ, {'AQ5_FORECAST_REQUEST':'1'})
    def test_confirmation_uses_original_ciphertext_and_refreshes_before_confirm(self):
        from aq5 import db, decision
        db.init()
        candidate = {'id':'c'*64,'code':'sh600000','route':'emotion','target_zone':[11,12]}
        original = {'code':'sh600000','fusion':{'routes':{'emotion':{'proposal':candidate}}}}
        source = {'id':'f'*32,'code':'sh600000','kind':'analysis',**worker._encrypt(original,'password')}
        item = {'id':'a'*32,'claim':'b'*32,'kind':'confirm','code':'sh600000','route':'emotion',
                'source_request_id':'f'*32,'proposal_id':'c'*64,'previous_id':None}
        events = []
        saved = {**candidate,'confirmed_at':'2026-11-11T10:01:00+08:00'}
        def refresh(code):events.append('refresh');return {'code':code}
        def remember(value):events.append('remember');self.assertEqual(value['routes']['emotion']['proposal'],candidate)
        def confirm(value):events.append('confirm');self.assertEqual(value['proposal_id'],'c'*64);return saved
        with patch.object(decision,'remember',side_effect=remember),patch.object(decision,'confirm_target',side_effect=confirm):
            payload = worker.fulfill(item,'password',None,context={'targets':[],'source':source},handlers={'analysis':refresh})
        self.assertEqual(events,['refresh','remember','confirm'])
        self.assertEqual(worker._decrypt(payload,'password')['confirmed_target'],saved)
        self.assertEqual(worker._decrypt(payload['target_update'],'password'),saved)
        self.assertEqual(payload['target_update']['previous_id'],None)
        with self.assertRaises(ValueError):
            worker.fulfill({**item,'proposal_id':'d'*64},'password',None,
                           context={'targets':[],'source':source},handlers={'analysis':refresh})
        with self.assertRaises(ValueError):
            worker.fulfill(item,'password',None,
                           context={'targets':[],'source':{**source,'id':'e'*32}},handlers={'analysis':refresh})

    @patch.dict(os.environ, {'AQ5_FORECAST_REQUEST':''})
    def test_research_mutations_require_explicit_isolated_mode(self):
        with self.assertRaises(RuntimeError):
            worker.fulfill({'id':'a'*32,'claim':'b'*32,'kind':'screen'},'password',None,
                           context={'targets':[]},handlers={'screen':lambda:{}})

    def test_research_cipher_hash_and_encrypted_identity_are_checked(self):
        from aq5 import db
        db.init()
        target = {'id':'c'*64,'code':'sz000001','route':'value','created_at':'2026-11-11T10:00:00+08:00',
                  'confirmed_at':'2026-11-11T10:01:00+08:00'}
        envelope={'id':target['id'],'code':'sh600000','route':'value',**worker._encrypt(target,'password')}
        with self.assertRaises(ValueError):worker._restore_targets('sh600000',[envelope],'password')
        with self.assertRaises(ValueError):worker._decrypt({**envelope,'sha256':'0'*64},'password')

    def test_result_is_encrypted_and_bound_to_stock_and_claim(self):
        item = {'id': 'a'*32, 'code': 'sz002911', 'claim': 'b'*32}
        body = worker.fulfill(item, 'test-password', lambda code: {'symbol': {'code': code}, 'summary': {'stance': '观察'}})
        cipher = base64.b64decode(body['data_b64'])
        self.assertEqual(hashlib.sha256(cipher).hexdigest(), body['sha256'])
        self.assertEqual(item['claim'], body['claim'])
        plain = json.loads(gzip.decompress(vault.unseal_web(cipher, 'test-password')))
        self.assertEqual(plain['symbol']['code'], item['code'])
        with self.assertRaises(ValueError):
            worker.fulfill(item, 'test-password', lambda _: {'symbol': {'code': 'sh600000'}})

    @patch.dict(os.environ, {'AQ5_KEY':'test-password', 'AQ5_RELAY':'https://example.test', 'AQ5_FORECAST_REQUEST':'1'})
    def test_failure_does_not_expose_provider_or_secret_and_next_job_runs(self):
        items = [{'id': c*32, 'claim':'c'*32, 'code': code} for c, code in [('a','sh600000'),('b','sz002911')]]
        completed = []
        def transport(relay, key, action, body=None):
            if action == 'poll':
                rows = items[:]
                items.clear()
                return {'requests':rows}
            completed.append(body)
            return {'ok':True}
        def analyze(code):
            if code == 'sh600000':
                raise RuntimeError('private-provider-response')
            return {'symbol':{'code':code}}
        self.assertEqual(worker.run(transport=transport, analyze=analyze), 2)
        self.assertTrue(completed[0]['error'])
        self.assertNotIn('private-provider-response', json.dumps(completed))
        self.assertIn('data_b64', completed[1])

    @patch.dict(os.environ, {'AQ5_KEY':'test-password', 'AQ5_RELAY':'https://example.test', 'AQ5_FORECAST_REQUEST':'0'})
    def test_requires_isolated_mode(self):
        with self.assertRaises(RuntimeError):
            worker.run(analyze=lambda code: {})

    @patch.dict(os.environ, {'AQ5_FORECAST_REQUEST':'1', 'AQ5_DATA':''})
    def test_main_rejects_missing_data_before_initialization(self):
        with self.assertRaisesRegex(RuntimeError, 'isolated'):
            worker.main()

    def test_transient_completion_retries_identical_payload_and_stale_claim_stops(self):
        calls = []
        payload = {'id':'a'*32,'claim':'b'*32,'error':True}
        def flaky(*args):
            calls.append(args)
            if len(calls) == 1:
                raise OSError('transport unavailable')
            return {'ok':True}
        worker.complete('https://example.test', 'key', payload, flaky, lambda seconds: None)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], calls[1])
        def expired(*args):
            raise urllib.error.HTTPError('https://example.test', 409, 'stale', {}, None)
        self.assertTrue(worker.complete('https://example.test', 'key', payload, expired, lambda seconds: None)['expired'])

    @patch.dict(os.environ, {'AQ5_KEY':'test-password', 'AQ5_RELAY':'https://example.test', 'AQ5_FORECAST_REQUEST':'1'})
    def test_expired_claim_does_not_abandon_next_claimed_job(self):
        items = [{'id':c*32,'claim':'c'*32,'code':'sh600000'} for c in ('a','b')]
        completed=[]
        def transport(relay, key, action, body=None):
            if action == 'poll':
                out=items[:];items.clear();return {'requests':out}
            completed.append(body['id'])
            if body['id'].startswith('a'):
                raise urllib.error.HTTPError('https://example.test', 409, 'stale', {}, None)
            return {'ok':True}
        self.assertEqual(worker.run(transport=transport,analyze=lambda code:{'symbol':{'code':code}}),2)
        self.assertEqual(completed,['a'*32,'b'*32])

    def test_model_restore_imports_no_account_or_settings(self):
        from aq5 import db, cloud
        db.init()
        before=db.q1('SELECT COUNT(*) AS n FROM accounts')['n']
        source=sqlite3.connect(':memory:')
        try:
            source.execute('CREATE TABLE accounts(id TEXT, cash REAL)')
            source.execute('INSERT INTO accounts VALUES(?,?)',('synthetic-private',123))
            source.execute('CREATE TABLE forecast_models(version,created,week,status,training_hash,artifact,metrics)')
            source.execute('INSERT INTO forecast_models VALUES(1,?,?,?,?,?,?)',('fixture','test-import','active','hash','{}','{}'))
            source.commit()
            body={'db':base64.b64encode(gzip.compress(source.serialize())).decode(),'settings':{'private':'do-not-import'}}
        finally:
            source.close()
        encrypted=vault.seal(json.dumps(body).encode(),'test-password')
        with patch.object(cloud,'read_remote_state',return_value=encrypted):
            worker.load_models('test-password')
        self.assertEqual(db.q1('SELECT COUNT(*) AS n FROM accounts')['n'],before)
        self.assertIsNone(db.q1("SELECT id FROM accounts WHERE id='synthetic-private'"))
        self.assertEqual(db.q1('SELECT training_hash FROM forecast_models WHERE version=1')['training_hash'],'hash')


if __name__ == '__main__':
    unittest.main()
