"""Read-only, after-hours analysis contract; synthetic data is not return evidence."""
import copy
import json
import math
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

base = Path(os.environ.get('AQ5_DATA', Path(__file__).resolve().parents[2] / 'forecast-test-data'))
base.mkdir(parents=True, exist_ok=True)
os.environ['AQ5_DATA'] = tempfile.mkdtemp(dir=base)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aq5 import db
from aq5.config import CN_TZ
from aq5.forecast import service, chan


def frames(at):
    output = {}
    for tf in service.LEVELS:
        step = timedelta(days=1) if tf == 'day' else timedelta(minutes=int(tf[:-1]))
        last = at.replace(hour=15, minute=0, second=0) - timedelta(days=1)
        rows = []
        for i in range(180):
            p = 50 + 8 * math.sin(i * .35) + 3 * math.sin(i * .08) + i * .015
            date = last - step * (179 - i)
            rows.append(dict(date=date.isoformat(), available_at=date.isoformat(), open=p-.1, close=p+.1,
                             high=p+.3, low=p-.3, vol=10000+i, source='synthetic-fixture',
                             adjustment='none', adjustment_verified=True))
        output[tf] = {'rows': rows, 'quality': {'eligible': True, 'reason': None}, 'source': 'synthetic-fixture'}
    return output


class AnalysisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init()
        cls.at = datetime(2026, 11, 14, 18, 0, tzinfo=CN_TZ)  # Saturday, no engine duty.
        cls.inputs = frames(cls.at)

    def call(self, *, at=None, supplied=None, quote=None, **kwargs):
        at = at or self.at
        supplied = supplied or self.inputs
        q = quote if quote is not None else {'code': 'sz002911', 'name': '佛燃能源', 'price': 50,
                                               'time': '2026-11-13 15:00:00', 'source': 'test'}
        return service.analyze('002911', at, feed=lambda c, tf, t: copy.deepcopy(supplied[tf]),
                               quote_loader=lambda codes: {'sz002911': q},
                               context_loader=lambda t, **k: {}, disclosure_loader=lambda c, t: None, **kwargs)

    def test_weekend_analysis_has_real_geometry_and_math_without_writes(self):
        before = '\n'.join(db.conn().iterdump())
        db.conn().execute('PRAGMA query_only=ON')
        try:
            with patch.object(db, 'tx', side_effect=AssertionError('analysis must not write')):
                result = self.call(model_loader=lambda: self.fail('after-hours structure does not need model state'))
        finally:
            db.conn().execute('PRAGMA query_only=OFF')
        self.assertEqual(before, '\n'.join(db.conn().iterdump()))
        self.assertEqual(result['symbol'], {'code': 'sz002911', 'name': '佛燃能源'})
        self.assertTrue(result['analysis_only'])
        self.assertEqual(len(result['structures']), 4)
        self.assertTrue(result['structures']['sz002911:day']['structures']['strokes'])
        self.assertIn('20日均线', result['summary']['summary'])
        self.assertIn('RSI(14)', result['summary']['summary'])
        self.assertIn('math_hurst', result['features'])
        self.assertEqual(result['predictions'], [])
        self.assertEqual(result['ledger'], {'books': {}})
        self.assertEqual(result['model_status']['versions'], [])
        json.dumps(result, allow_nan=False)

    def test_unverified_data_does_not_become_confirmed_direction_or_price_levels(self):
        inputs = copy.deepcopy(self.inputs)
        inputs['day']['quality'] = {'eligible': False, 'reason': 'adjustment_unverified'}
        result = self.call(supplied=inputs)
        self.assertFalse(result['summary']['quality']['eligible'])
        self.assertEqual(result['summary']['stance'], 'waiting')
        self.assertIsNone(result['summary']['support'])
        self.assertIsNone(result['summary']['resistance'])
        self.assertTrue(any('复权' in s for s in result['summary']['warnings']))
        self.assertNotIn('20日均线', result['summary']['summary'])
        self.assertFalse(result['structures']['sz002911:day']['quality']['eligible'])

    def test_one_level_failure_keeps_other_level_analysis(self):
        def feed(code, tf, asof):
            if tf == '5m':
                raise RuntimeError('upstream failure')
            return copy.deepcopy(self.inputs[tf])
        result = service.analyze('sz002911', self.at, feed=feed,
                                 quote_loader=lambda c: {}, context_loader=lambda t, **k: {},
                                 disclosure_loader=lambda c, t: None)
        self.assertIn('sz002911:day', result['structures'])
        self.assertNotIn('sz002911:5m', result['structures'])
        self.assertIn('5分钟', ' '.join(result['summary']['warnings']))
        self.assertIsNone(result['quote'])
        self.assertIsNotNone(result['last_completed_close'])

    def test_no_overnight_intraday_target_and_no_untrained_probability(self):
        at = datetime(2026, 11, 13, 14, 45, tzinfo=CN_TZ)
        quote = {'code': 'sz002911', 'name': '佛燃能源', 'price': 50,
                 'source_time': at.isoformat(), 'known_at': at.isoformat(), 'source': 'test'}
        loads = []
        result = self.call(at=at, quote=quote, model_loader=lambda: loads.append(True))
        self.assertTrue(result['predictions'])
        self.assertEqual(loads, [True])
        self.assertEqual({r['horizon'] for r in result['predictions']}, {'1d', '3d', '5d', '20d'})
        self.assertTrue(all(p['probability'] is None and not p['validated'] for p in result['predictions']))
        self.assertFalse(service._table('forecast_predictions'))
        self.assertFalse(service._table('forecast_models'))

    def test_bad_codes_fail_before_data_access(self):
        for code in ('', '浦发银行', 'sh000001', 'sz300001', 'sh002911', '../../settings', '002911?q=1'):
            with self.subTest(code=code), self.assertRaises(ValueError):
                service.analyze(code, feed=lambda *a: self.fail('should reject before network'))

    def test_live_acquisition_uses_post_fetch_clock(self):
        seen = []
        def feed(code, tf, asof):
            seen.append(asof)
            return copy.deepcopy(self.inputs[tf])
        result = service.analyze('002911', feed=feed, clock=lambda: self.at,
                                 quote_loader=lambda c: {}, context_loader=lambda t, **k: {},
                                 disclosure_loader=lambda c, t: None)
        self.assertEqual(seen, [None] * 4)
        self.assertEqual(result['summary']['updated'], self.at.isoformat())


if __name__ == '__main__':
    unittest.main()
