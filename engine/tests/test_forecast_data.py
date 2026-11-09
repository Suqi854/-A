import copy
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
from aq5 import db, market
from aq5.config import CN_TZ
from aq5.forecast import data, features
from aq5.forecast.calendar import official_days


def bar(date, price=10):
    return dict(date=date, open=price, close=price+.1, high=price+.2, low=price-.1, vol=10000)


def feed(rows, t):
    return {'rows': rows, 'source': 'test-observed-source', 'bar_time': 'end', 'observed_at': t.isoformat(),
            'adjustment': 'pit', 'adjustment_verified': True, 'adjustment_asof': t.isoformat(),
            'adjustment_evidence': {'verified': True, 'input_hash': 'test-only', 'method': 'test-fixture', 'observed_at': t.isoformat()}}


class DataContractTests(unittest.TestCase):
    def setUp(self):
        self.t = datetime(2026, 11, 9, 10, 7, tzinfo=CN_TZ)
        self.minute = [bar(e.isoformat()) for d in ('2026-11-05', '2026-11-06', '2026-11-09')
                       for e in data._ends(d, 5) if e <= self.t]
        self.daily = [bar(d) for d in official_days(2026) if d < '2026-11-09'][-80:]
        data._cache.clear(); data._adjustments.clear(); data._context_cache.clear()
        db.init()

    def test_cache_read_does_not_refresh_first_available_time(self):
        f = feed(self.minute, self.t)
        first = data.normalize(f, '5m', self.t)
        second = data.normalize(f, '5m', self.t + timedelta(seconds=30))
        self.assertTrue(first['quality']['eligible'], first['quality'])
        self.assertEqual(first['rows'][-1]['available_at'], second['rows'][-1]['available_at'])
        self.assertEqual(first['observed_at'], second['observed_at'])

    def test_future_fetch_is_not_backdated_to_prediction(self):
        f = feed(self.minute, self.t + timedelta(seconds=2))
        result = data.normalize(f, '5m', self.t)
        self.assertFalse(result['quality']['eligible'])
        self.assertEqual(result['rows'], [])
        self.assertIn('not_available_at_asof', result['quality']['reason'])

    def test_forming_bar_excluded_without_poisoning_previous_complete_bar(self):
        rows = self.minute + [bar('2026-11-09T10:10:00+08:00')]
        result = data.normalize(feed(rows, self.t), '5m', self.t)
        self.assertTrue(result['quality']['eligible'], result['quality'])
        self.assertEqual(result['rows'][-1]['date'], '2026-11-09T10:05:00+08:00')
        daily = data.normalize(feed(self.daily + [bar('2026-11-09')], self.t), 'day', self.t)
        self.assertTrue(daily['quality']['eligible'], daily['quality'])
        self.assertTrue(daily['rows'][-1]['date'].startswith('2026-11-06'))

    def test_lunch_and_first_hour_use_actual_last_completed_bar(self):
        noon = self.t.replace(hour=13, minute=3)
        rows = [bar(e.isoformat()) for d in official_days(2026) if '2026-10-09' <= d <= '2026-11-09'
                for e in data._ends(d, 60) if e <= noon]
        result = data.normalize(feed(rows, noon), '60m', noon)
        self.assertTrue(result['quality']['eligible'], result['quality'])
        self.assertEqual(result['rows'][-1]['date'], '2026-11-09T11:30:00+08:00')

    def test_missing_entire_day_and_wrong_bar_alignment_fail_closed(self):
        rows = [r for r in self.minute if not r['date'].startswith('2026-11-06')]
        result = data.normalize(feed(rows, self.t), '5m', self.t)
        self.assertIn('missing_bars', result['quality']['reason'])
        rows = self.minute + [bar('2026-11-09T10:02:00+08:00')]
        result = data.normalize(feed(rows, self.t), '5m', self.t)
        self.assertIn('invalid_session_bar_end', result['quality']['reason'])

    def test_just_metadata_cannot_claim_adjustment_verified(self):
        f = feed(self.minute, self.t); f.pop('adjustment_evidence')
        result = data.normalize(f, '5m', self.t)
        self.assertIn('adjustment_unverified', result['quality']['reason'])

    def test_dividend_adjustment_is_affine_not_close_ratio(self):
        raw = bar('2026-11-06', 10)
        adjusted = {**raw, **{k: raw[k]-.42 for k in data.PRICE_FIELDS}}
        evidence = data._affine_evidence({'bfq': [raw], 'qfq': [adjusted], 'observed_at': self.t.isoformat()})
        f = evidence['days']['2026-11-06']
        self.assertAlmostEqual(f['a'], 1)
        self.assertAlmostEqual(f['b'], -.42)
        self.assertAlmostEqual(f['a'] * raw['open'] + f['b'], adjusted['open'])
        self.assertNotAlmostEqual(raw['open'] * adjusted['close'] / raw['close'], adjusted['open'])

    def test_inconsistent_ohlc_adjustment_rejected(self):
        raw = bar('2026-11-06', 10)
        adjusted = {**raw, 'open': 8, 'close': 9, 'high': 20, 'low': 2}
        with self.assertRaisesRegex(ValueError, 'no_verified_adjustment_days'):
            data._affine_evidence({'bfq': [raw], 'qfq': [adjusted], 'observed_at': self.t.isoformat()})

    def test_raw_payload_cannot_be_relabelled_as_qfq(self):
        payload = {'data': {'sh600000': {'day': [['2026-11-06', '10', '10', '11', '9', '100']]}}}
        import json
        with patch.object(market, 'http_get', return_value=json.dumps(payload).encode()):
            with self.assertRaisesRegex(ValueError, 'explicit_daily_basis_missing'):
                data._daily_pair('sh600000')

    def test_real_time_fetch_observes_after_acquisition(self):
        pair = {'qfq': self.daily, 'bfq': self.daily, 'observed_at': self.t.isoformat()}
        with patch.object(data, 'MOCK', False), patch.object(data, 'now', return_value=self.t), patch.object(data, '_daily_pair', return_value=pair):
            result = data.fetch('sh600000', 'day')
            self.assertTrue(result['quality']['eligible'], result['quality'])
            past = data.fetch('sh600000', 'day', self.t-timedelta(seconds=1))
            self.assertFalse(past['quality']['eligible'])


class ContextEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.t = datetime(2026, 11, 9, 10, 7, tzinfo=CN_TZ)
        db.init()
        with db.tx() as c:
            c.execute('CREATE TABLE IF NOT EXISTS stock_sector(code TEXT PRIMARY KEY,industry TEXT,ind_code TEXT,updated TEXT)')
            c.execute('CREATE TABLE IF NOT EXISTS ann_seen(key TEXT PRIMARY KEY,code TEXT,name TEXT,title TEXT,ts TEXT,tag TEXT)')
            c.execute('DELETE FROM stock_sector'); c.execute('DELETE FROM ann_seen')
            for i in range(3):
                c.execute('INSERT INTO stock_sector VALUES(?,?,?,?)', ('sh60000'+str(i), 'bank', 'bank', '2026-11-06'))
            c.execute('INSERT INTO ann_seen VALUES(?,?,?,?,?,?)', ('fixture', 'sh600000', 'fixture', '减持公告', '2026-11-09 09:30:00', 'risk'))

    def test_received_at_does_not_replace_source_quote_time(self):
        quote = {'code': 'sh000001', 'source': 'test', 'received_at': self.t.isoformat(), 'pct': 1}
        with patch.dict(market._cache, {}, clear=True), patch.object(market, 'quotes_recent', return_value={'sh000001': quote}), patch.object(data, 'now', return_value=self.t):
            result = data.context(self.t)
        self.assertNotIn('market', result)
        self.assertEqual(result['_coverage']['market']['status'], 'unavailable')

    def test_market_sector_flow_and_event_have_distinct_time_evidence(self):
        rows = [{'code': 'sh60000'+str(i), 'time': self.t.isoformat(), 'pct': i-1, 'amount': 10000} for i in range(3)]
        index = {'code': 'sh000001', 'source': 'test', 'time': self.t.isoformat(), 'pct': .5}
        flow = {'source': 'daily-test', 'source_time': '2026-11-06', 'source_precision': 'date', 'granularity': 'day',
                'known_at': self.t.isoformat(), 'values': {'main_last_yuan': 1000}}
        with patch.dict(market._cache, {'snapshot': (self.t.timestamp(), rows)}, clear=True), patch.object(market, 'quotes_recent', return_value={'sh000001': index}), patch.object(data, '_flow', return_value=flow), patch.object(data, 'now', return_value=self.t):
            result = data.context(self.t, code='sh600000')
        self.assertEqual(set(result)-{'_coverage'}, {'market', 'sector', 'flow', 'event'})
        self.assertEqual(result['market']['coverage'], 'indices_and_subset')
        self.assertEqual(result['flow']['source_time'], '2026-11-06')
        self.assertEqual(result['event']['values']['risk_title_count24h'], 1)
        built = features.build([], asof=self.t, context=result)
        for family in ('flow', 'market', 'sector', 'event'):
            self.assertEqual(built['values']['missing_'+family], 0)


if __name__ == '__main__':
    unittest.main()
