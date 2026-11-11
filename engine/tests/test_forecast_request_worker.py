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
