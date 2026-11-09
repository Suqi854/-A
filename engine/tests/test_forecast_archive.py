"""Synthetic-only encrypted history tests; no real remote, secrets, or orders."""
import copy
import gzip
import hashlib
import json
import os
import random
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

base = Path(os.environ.get('AQ5_DATA', Path(__file__).resolve().parents[2] / 'forecast-test-data'))
base.mkdir(parents=True, exist_ok=True)
os.environ['AQ5_DATA'] = tempfile.mkdtemp(dir=base)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aq5 import db, vault
from aq5.forecast import archive, ledger

KEY = 'synthetic-test-key-never-production'
COMMIT = 'a' * 40
CFG = {'repo': 'test-owner/test-repo', 'remote': 'https://github.com/test-owner/test-repo.git'}


class FakeGit:
    def __init__(self, fail_push=False, mismatch=False):
        self.calls = []
        self.pushed = False
        self.fail_push = fail_push
        self.mismatch = mismatch

    def __call__(self, args, cwd, timeout=90):
        self.calls.append(list(args))
        if args[0] == 'remote' and len(args) == 1:
            return 0, ''
        if args[0] == 'ls-remote':
            if not self.pushed:
                return 2, ''
            return 0, ('b' * 40 if self.mismatch else COMMIT) + '\trefs/heads/forecast-history'
        if args[0] == 'rev-parse':
            return 0, COMMIT
        if args[0] == 'push':
            if self.fail_push:
                return 1, 'PRIVATE_REMOTE_TOKEN_MUST_NOT_ESCAPE'
            self.pushed = True
        return 0, ''


def prediction(i=1, day='2026-11-06', features=120):
    start = day + 'T10:00:00+08:00'
    return {'id': 'p-' + str(i), 'code': 'sh600000', 'horizon': '1d', 'asof': start,
            'due_at': '2026-11-10T15:00:00+08:00', 'variant': 'fusion', 'model_version': '1',
            'origin': 'forward', 'reference_price': 10, 'source_time': start,
            'probability': .6, 'quantiles': {'p10': -.02, 'p50': .005, 'p90': .03},
            'features': {'feature-' + str(j): (i * 7 + j) / 123 for j in range(features)},
            'coverage': [{'id': 'family-' + str(j), 'status': 'computed', 'description': 'synthetic-only-measurement'} for j in range(12)],
            'quality': {'eligible': True}}


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        db.init(); ledger.init(); archive.init()
        with db.tx() as c:
            c.execute('DELETE FROM forecast_predictions'); c.execute('DELETE FROM forecast_results'); c.execute('DELETE FROM forecast_archives')
            c.execute('CREATE TABLE IF NOT EXISTS forecast_structures(id TEXT PRIMARY KEY,code TEXT,timeframe TEXT,asof TEXT,body TEXT)')
            c.execute('DELETE FROM forecast_structures')
        self.root = Path(tempfile.mkdtemp(dir=base)) / 'forecast-history'
        self.root_patch = patch.object(archive, 'ROOT', self.root)
        self.root_patch.start(); self.addCleanup(self.root_patch.stop)
        self.key_patch = patch.object(archive.cloud, '_key', return_value=KEY)
        self.key_patch.start(); self.addCleanup(self.key_patch.stop)
        self.cfg_patch = patch.object(archive.cloud, '_cfg', return_value=CFG)
        self.cfg_patch.start(); self.addCleanup(self.cfg_patch.stop)
        archive._decoded.clear(); archive._disk_partitions.clear()
        self.git = FakeGit()

    def run_compact(self, git=None):
        with patch.object(archive, '_git', git or self.git):
            return archive.compact('2026-11-09T16:10:00+08:00')

    def test_round_trip_exact_hash_and_confirmed_push_before_compaction(self):
        item = prediction(); ledger.record_prediction(item)
        original = db.q1('SELECT body,hash FROM forecast_predictions')['body']
        original_hash = db.q1('SELECT hash FROM forecast_predictions')['hash']
        result = self.run_compact()
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['archived_records'], 1)
        compact = db.q1('SELECT body,hash FROM forecast_predictions')
        self.assertEqual(compact['hash'], original_hash)
        self.assertNotIn('features', json.loads(compact['body']))
        self.assertEqual(archive.read_record(compact['body']), json.loads(original))
        self.assertEqual(archive._hash(archive.read_record(compact['body'])), original_hash)
        self.assertEqual(db.q1('SELECT commit_sha FROM forecast_archives')['commit_sha'], COMMIT)
        calls = [c[0] for c in self.git.calls]
        self.assertLess(calls.index('push'), len(calls)-1)
        self.assertEqual(calls[-1], 'ls-remote')
        self.assertFalse(any('--force' in c for c in self.git.calls))

    def test_push_failure_never_compacts_or_leaks_git_error(self):
        ledger.record_prediction(prediction())
        before = db.q1('SELECT body FROM forecast_predictions')['body']
        result = self.run_compact(FakeGit(fail_push=True))
        self.assertFalse(result['ok'])
        self.assertNotIn('PRIVATE_REMOTE_TOKEN', json.dumps(result))
        self.assertEqual(db.q1('SELECT body FROM forecast_predictions')['body'], before)
        self.assertEqual(db.q1('SELECT count(*) AS n FROM forecast_archives')['n'], 0)

    def test_unconfirmed_remote_commit_never_compacts(self):
        ledger.record_prediction(prediction())
        before = db.q1('SELECT body FROM forecast_predictions')['body']
        result = self.run_compact(FakeGit(mismatch=True))
        self.assertFalse(result['ok'])
        self.assertEqual(result['error'], 'archive_push_unconfirmed')
        self.assertEqual(db.q1('SELECT body FROM forecast_predictions')['body'], before)

    def test_only_ciphertext_uploaded_and_no_plaintext_local_history(self):
        item = prediction(); item['features']['PRIVATE_SENTINEL_82193'] = 123
        ledger.record_prediction(item)
        result = self.run_compact(); self.assertTrue(result['ok'], result)
        for path in self.root.rglob('*'):
            if path.is_file():
                raw = path.read_bytes()
                self.assertNotIn(b'PRIVATE_SENTINEL_82193', raw)
                self.assertNotIn(KEY.encode(), raw)
                if path.suffix == '.bin':
                    self.assertTrue(raw.startswith(b'AQ5v1'))
        added = [x for c in self.git.calls if c[0] == 'add' for x in c[2:]]
        self.assertTrue(added)
        self.assertTrue(all(x.startswith('days/') and x.endswith('.bin') for x in added))

    def test_restart_resolver_downloads_immutable_cipher_and_verifies_it(self):
        item = prediction(); ledger.record_prediction(item); result = self.run_compact()
        self.assertTrue(result['ok'], result)
        body = db.q1('SELECT body FROM forecast_predictions')['body']; ref = json.loads(body)['_archive_ref']
        cipher = (self.root/'cache'/ref['partition']/(ref['cipher_hash']+'.bin')).read_bytes()
        archive._decoded.clear()
        fresh_root = Path(tempfile.mkdtemp(dir=base))/'restart'
        with patch.object(archive, 'ROOT', fresh_root), patch.object(archive, '_remote_cipher', return_value=cipher) as remote:
            restored = archive.read_record(body)
            self.assertEqual(restored['features'], item['features'])
            self.assertEqual(remote.call_args.args[0]['commit'], COMMIT)
            # The second read is offline and uses verified partition cache.
            archive._decoded.clear()
            with patch.object(archive, '_remote_cipher', side_effect=AssertionError('network unnecessary')):
                self.assertEqual(archive.read_record(body), restored)

    def test_tampered_cipher_key_or_reference_fails_closed(self):
        ledger.record_prediction(prediction()); self.assertTrue(self.run_compact()['ok'])
        body = db.q1('SELECT body FROM forecast_predictions')['body']; ref = json.loads(body)['_archive_ref']
        path = self.root/'cache'/ref['partition']/(ref['cipher_hash']+'.bin')
        path.write_bytes(path.read_bytes()[:-1]+b'x')
        with self.assertRaisesRegex(archive.ArchiveError, 'cipher_hash_mismatch'):
            archive.read_record(body)
        with patch.object(archive.cloud, '_key', return_value='wrong-key'):
            with self.assertRaisesRegex(archive.ArchiveError, 'key_fingerprint_mismatch'):
                archive.read_record(body)
        forged = json.loads(body); forged['_archive_ref']['path'] = '../../vault.bin'
        with self.assertRaisesRegex(archive.ArchiveError, 'invalid_archive_reference'):
            archive.read_record(forged)

    def test_today_and_latest_structure_remain_full(self):
        ledger.record_prediction(prediction(1, '2026-11-09'))
        with db.tx() as c:
            for day in ('2026-11-05', '2026-11-06'):
                item = {'id': day, 'code':'sh600000', 'timeframe':'day', 'asof': day+'T15:00:00+08:00', 'structures':{'strokes':[{'x': 1}]}, 'features':{'chan':1}}
                c.execute('INSERT INTO forecast_structures VALUES(?,?,?,?,?)', (day,'sh600000','day',item['asof'],json.dumps(item)))
        result=self.run_compact(); self.assertTrue(result['ok'],result)
        self.assertEqual(result['archived_records'],1)
        self.assertNotIn('_archive_ref',json.loads(db.q1('SELECT body FROM forecast_predictions')['body']))
        latest=json.loads(db.q1("SELECT body FROM forecast_structures WHERE id='2026-11-06'")['body'])
        self.assertNotIn('_archive_ref',latest)
        old=db.q1("SELECT body FROM forecast_structures WHERE id='2026-11-05'")['body']
        self.assertEqual(archive.read_record(old)['structures'],latest['structures'])

    def test_shards_enforce_cipher_and_plaintext_limits(self):
        rng=random.Random(32); records=[]
        for i in range(20):
            item={'id':str(i),'features':{'random': ''.join(rng.choice('abcdef0123456789') for _ in range(1600))}}
            records.append({'id':str(i),'table':'forecast_predictions','partition':'2026-11-06','body':archive._json(item),'hash':archive._hash(item)})
        shards=archive._build_shards(records,KEY,max_cipher_bytes=1800,max_plain_bytes=4096)
        self.assertGreater(len(shards),1)
        self.assertTrue(all(len(s['cipher'])<=1800 and s['plain_bytes']<=4096 for s in shards))
        self.assertEqual(sum(len(s['records']) for s in shards),len(records))
        with self.assertRaisesRegex(archive.ArchiveError,'single_archive_record_exceeds_limit'):
            archive._build_shards(records,KEY,max_cipher_bytes=70,max_plain_bytes=4096)

    def test_training_and_history_queries_resolve_archived_features(self):
        ledger.record_prediction(prediction())
        with db.tx() as c:
            c.execute('INSERT INTO forecast_results VALUES(?,?,?,?,?)',('p-1','2026-11-10T15:00:00+08:00','settled',.01,'{}'))
        self.assertTrue(self.run_compact()['ok'])
        self.assertEqual(ledger.predictions()[0]['features'],prediction()['features'])
        self.assertEqual(ledger.training_rows()[0]['features'],prediction()['features'])
        self.assertEqual(db.q1('SELECT count(*) AS n FROM forecast_results')['n'],1)

    def test_missing_key_does_not_archive(self):
        ledger.record_prediction(prediction()); before=db.q1('SELECT body FROM forecast_predictions')['body']
        with patch.object(archive.cloud,'_key',return_value=''):
            result=self.run_compact()
        self.assertFalse(result['ok'])
        self.assertEqual(db.q1('SELECT body FROM forecast_predictions')['body'],before)

    def test_sqlite_failure_rolls_back_refs_and_manifest_together(self):
        ledger.record_prediction(prediction()); before=db.q1('SELECT body FROM forecast_predictions')['body']
        with db.tx() as c:
            c.execute("CREATE TRIGGER archive_test_abort BEFORE UPDATE ON forecast_predictions BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
        try:
            result=self.run_compact()
            self.assertFalse(result['ok'])
            self.assertEqual(db.q1('SELECT body FROM forecast_predictions')['body'],before)
            self.assertEqual(db.q1('SELECT count(*) AS n FROM forecast_archives')['n'],0)
        finally:
            with db.tx() as c:
                c.execute('DROP TRIGGER archive_test_abort')

    def test_disk_cache_partition_bound_survives_process_memory_reset(self):
        for i,day in enumerate(('2026-11-04','2026-11-05','2026-11-06')):
            cipher=vault.seal(gzip.compress(json.dumps({'synthetic':i}).encode()),KEY)
            archive._disk_partitions.clear()
            archive._store_cipher(hashlib.sha256(cipher).hexdigest(),day,cipher)
        partitions={p.parent.name for p in (self.root/'cache').glob('*/*.bin')}
        self.assertLessEqual(len(partitions),archive.MAX_CACHE_PARTITIONS)
        self.assertIn('2026-11-06',partitions)

    def test_cached_payload_does_not_bypass_reference_integrity(self):
        ledger.record_prediction(prediction()); self.assertTrue(self.run_compact()['ok'])
        body=json.loads(db.q1('SELECT body FROM forecast_predictions')['body'])
        archive.read_record(body)
        forged=copy.deepcopy(body);forged['_archive_ref']['payload_hash']='f'*64
        with self.assertRaisesRegex(archive.ArchiveError,'cached_reference_mismatch'):
            archive.read_record(forged)

    def test_measured_compaction_reduces_large_feature_bodies(self):
        for i in range(250):
            ledger.record_prediction(prediction(i))
        result=self.run_compact()
        self.assertTrue(result['ok'],result)
        self.assertEqual(result['archived_records'],250)
        self.assertLess(result['after_bytes'],result['before_bytes']*.4)
        print('ARCHIVE_MEASUREMENT',json.dumps({k:result[k] for k in ('archived_records','before_bytes','after_bytes','cipher_bytes','shards')},sort_keys=True))


if __name__=='__main__':
    unittest.main()
