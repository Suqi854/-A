"""Verified current industry projection and independent-context counterexamples."""
import copy
import gzip
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

os.environ['AQ5_DATA'] = os.environ['AQ5_TEST_DATA']
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aq5 import decision_industry as d
from aq5 import decision_emotion as em

AT = '2026-11-12T13:00:00+08:00'


class IndustryTests(unittest.TestCase):
    def setUp(self):
        Path(os.environ['AQ5_TEST_DATA']).mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ['AQ5_TEST_DATA'])
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for mod in (d, em):
            p = patch.object(mod, 'DATA', self.root); p.start(); self.addCleanup(p.stop)
        self.codes = [f'sh60000{x}' for x in range(5)]
        self.universe = {'verified': True, 'asof': AT, 'rows': [{'code': c} for c in self.codes]}
        self.rows = [{'SECURITY_CODE': c[2:], 'REPORT_DATE': '2026-09-30', 'INDUSTRY_NAME': '测试行业'} for c in self.codes]

    def growth(self, rows=None):
        raw = json.dumps({'success': True, 'result': {'data': self.rows if rows is None else rows}}).encode()
        digest = d._sha(raw)
        path = self.root / 'decision-sources' / 'growth' / (digest + '.json')
        path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(raw)
        return {'sources': [{'source_url': 'https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_DMSK_FN_INCOME',
                             'raw_sha256': digest, 'raw_path': str(path), 'retrieved_at': AT}]}

    def emotion(self, bars=61):
        emotion = {'run_id': 'test-run', 'asof': AT, 'version': em.FULL_VERSION, 'records': [], 'sources': [],
                   'coverage': {'membership_unmapped_count': 5, 'data_complete': False}, 'contexts': {},
                   'unmapped_codes': self.codes[:], 'status': 'complete_with_gaps',
                   'code_outcomes': {c: {'membership_covered': False, 'industry_mapping': 'source_incomplete',
                        'industry_contexts': [], 'history': 'qualified' if bars >= 61 else 'insufficient_history',
                        'lhb': 'not_listed_in_complete_window', 'mainline': 'UNKNOWN', 'core': 'UNKNOWN', 'cooling': 'UNKNOWN'} for c in self.codes}}
        folder = self.root / 'decision-public' / 'emotion'
        run = folder / 'full' / emotion['run_id']; (run / 'tasks').mkdir(parents=True)
        (run / 'manifest.json').write_text(json.dumps({'scope_hash': em._sha(em._json_bytes(sorted(self.codes))), 'version': em.FULL_VERSION, 'asof': AT}))
        for code in ['sh000300', *self.codes]:
            raw = ('raw-history-' + code).encode(); digest = d._sha(raw)
            raw_path = folder / (digest + '.json'); raw_path.write_bytes(raw)
            series = [{'date': (datetime(2026, 11, 11) - timedelta(days=bars - i - 1)).date().isoformat(),
                       'close': 10 * 1.02 ** i, 'open': 10 * 1.02 ** i, 'high': 10 * 1.02 ** i,
                       'low': 10 * 1.02 ** i, 'vol': 100} for i in range(bars)]
            receipt = {'raw_path': str(raw_path), 'raw_sha256': digest, 'retrieved_at': AT, 'source_url': 'https://example.test/history'}
            result = {'status': 'ok', 'value': [series, receipt]}
            key = 'history:' + code
            saved = {'key': key, 'result': result, 'checksum': em._sha(em._json_bytes(result))}
            (run / 'tasks' / (em._sha(key.encode()) + '.json.gz')).write_bytes(gzip.compress(em._json_bytes(saved)))
        return emotion

    def test_full_scope_mapping_observed_not_backdated(self):
        result = d.project(self.universe, self.growth())
        self.assertTrue(result['complete']); self.assertEqual(result['coverage']['mapped_count'], 5)
        self.assertEqual(result['codes'][self.codes[0]]['observed_at'], AT)
        self.assertEqual(result['codes'][self.codes[0]]['selected_report_date'], '2026-09-30')
        self.assertIn('不回溯', result['note'])

    def test_latest_period_selected_without_growth_metrics(self):
        rows = self.rows + [dict(self.rows[0], REPORT_DATE='2025-12-31', INDUSTRY_NAME='旧分类')]
        result = d.project(self.universe, self.growth(rows))
        self.assertTrue(result['complete']); self.assertNotIn('旧分类', result['groups'])

    def test_latest_duplicate_conflict_unknown(self):
        result = d.project(self.universe, self.growth(self.rows + [dict(self.rows[0], INDUSTRY_NAME='冲突')]))
        self.assertFalse(result['complete']); self.assertEqual(result['unknown'][self.codes[0]], 'latest_period_industry_conflict')

    def test_missing_or_empty_not_complete(self):
        for rows in (self.rows[1:], [dict(r, INDUSTRY_NAME='') for r in self.rows]):
            self.assertFalse(d.project(self.universe, self.growth(rows))['complete'])

    def test_outside_scope_not_silently_complete(self):
        result = d.project(self.universe, self.growth(self.rows + [dict(self.rows[0], SECURITY_CODE='600999')]))
        self.assertFalse(result['complete']); self.assertEqual(result['outside_scope'], ['sh600999'])

    def test_unverified_or_duplicate_official_scope(self):
        for u in (dict(self.universe, verified=False), dict(self.universe, rows=self.universe['rows'] * 2)):
            self.assertFalse(d.project(u, self.growth())['complete'])

    def test_raw_tamper_blocks_projection(self):
        g = self.growth(); Path(g['sources'][0]['raw_path']).write_text('{}')
        self.assertFalse(d.project(self.universe, g)['complete'])

    def test_augments_mapping_preserves_sina_gaps_and_inputs(self):
        e = self.emotion(); original = copy.deepcopy(e)
        result = d.augment(self.universe, self.growth(), e)
        self.assertEqual(e, original)
        self.assertEqual(result['coverage']['membership_covered'], 5)
        self.assertEqual(result['sina_coverage']['membership_unmapped_count'], 5)
        self.assertEqual(result['sina_unmapped_codes'], self.codes)
        self.assertFalse(result['coverage']['data_complete'])
        self.assertTrue(all(r['provenance']['context_id'].startswith('financial-industry:') for r in result['records']))
        self.assertTrue(all('新浪' not in r['provenance']['method'] for r in result['records']))

    def test_small_group_retains_unknown_threshold(self):
        self.codes = self.codes[:4]; self.universe['rows'] = self.universe['rows'][:4]; self.rows = self.rows[:4]
        result = d.augment(self.universe, self.growth(), self.emotion())
        self.assertEqual(result['records'], [])
        self.assertTrue(all(o['core'] == 'UNKNOWN' for o in result['code_outcomes'].values()))

    def test_complete_histories_produce_true_and_false_same_context(self):
        result = d.augment(self.universe, self.growth(), self.emotion())
        self.assertEqual(len(result['records']), 15)
        for code in self.codes:
            claims = [r for r in result['records'] if r['code'] == code]
            self.assertEqual({r['kind']: r['value'] for r in claims}, {'mainline': True, 'core': False, 'cooling': False})
            self.assertEqual(len({r['provenance']['context_id'] for r in claims}), 1)
            self.assertTrue(all(r['available_at'] >= AT for r in claims))

    def test_short_history_does_not_invent_core(self):
        result = d.augment(self.universe, self.growth(), self.emotion(bars=20))
        self.assertTrue(result['records'])
        self.assertTrue(all(r['kind'] == 'mainline' for r in result['records']))

    def test_history_raw_tamper_blocks_context(self):
        e = self.emotion()
        raw = self.root / 'decision-public' / 'emotion' / (d._sha(('raw-history-' + self.codes[0]).encode()) + '.json')
        raw.write_text('tampered')
        result = d.augment(self.universe, self.growth(), e)
        self.assertEqual(result['records'], [])

    def test_context_choice_never_splices_core(self):
        e = self.emotion()
        e['records'] = [{'code': self.codes[0], 'kind': 'mainline', 'value': True, 'provenance': {'context_id': 'sina-original'}},
                        {'code': self.codes[0], 'kind': 'core', 'value': True, 'provenance': {'context_id': 'sina-original'}},
                        {'code': self.codes[0], 'kind': 'cooling', 'value': True, 'provenance': {'context_id': 'sina-original'}}]
        result = d.augment(self.universe, self.growth(), e)
        claims = [r for r in result['records'] if r['code'] == self.codes[0]]
        self.assertEqual(len({r['provenance']['context_id'] for r in claims}), 1)

    def test_checkpoint_scope_mismatch_is_unknown(self):
        e = self.emotion(); e['asof'] = '2026-11-12T14:00:00+08:00'
        result = d.augment(self.universe, self.growth(), e)
        self.assertEqual(result['financial_industry']['evaluation_status'], 'UNKNOWN')

    def test_record_receipts_scale_with_own_sources_not_entire_group(self):
        growth = {'sources': [self.growth([row])['sources'][0] for row in self.rows]}
        growth['sources'][-1]['retrieved_at'] = '2026-11-12T14:00:00+08:00'
        result = d.augment(self.universe, growth, self.emotion())
        own_hash = growth['sources'][0]['raw_sha256']
        for record in result['records']:
            receipts = record['provenance']['raw_receipts']
            self.assertEqual(len(receipts), 2)
            self.assertEqual(record['available_at'], '2026-11-12T14:00:00+08:00')
            if record['code'] == self.codes[0]:
                self.assertEqual(receipts[0]['raw_sha256'], own_hash)
            proof = result['contexts'][record['provenance']['context_id']]
            self.assertEqual(len(proof['membership_refs']), 5)
            self.assertEqual(proof['proof_sha256'], record['provenance']['context_evidence_sha256'])


if __name__ == '__main__': unittest.main(verbosity=2)
