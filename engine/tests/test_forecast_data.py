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

    def test_day_identity_requires_two_matching_responses_and_retains_basis(self):
        payload = {'data': {'sh600000': {'day': [['2026-11-06', '10', '10', '11', '9', '100']]}}}
        import json
        with patch.object(market, 'http_get', return_value=json.dumps(payload).encode()) as request:
            pair = data._daily_pair('sh600000')
        self.assertEqual(request.call_count, 2)
        self.assertEqual(pair['basis'], 'qfq_request_day_crosschecked_identity')
        self.assertEqual(pair['response_fields'], {'qfq': 'day', 'bfq': 'day'})
        self.assertNotEqual(pair['basis'], 'explicit_qfq')

    def test_day_identity_rejects_different_dates_prices_or_volume(self):
        import json
        good = {'data': {'sh600000': {'day': [['2026-11-06', '10', '10', '11', '9', '100']]}}}
        for index, value, error in ((0, '2026-11-05', 'dates_mismatch'), (1, '9.5', 'ohlc_mismatch'), (5, '200', 'volume_mismatch')):
            bad = copy.deepcopy(good); bad['data']['sh600000']['day'][0][index] = value
            with patch.object(market, 'http_get', side_effect=[json.dumps(good).encode(), json.dumps(bad).encode()]):
                with self.assertRaisesRegex(ValueError, error):
                    data._daily_pair('sh600000')

    def test_missing_both_daily_fields_remains_unverified(self):
        import json
        payload = {'data': {'sh600000': {'version': 'test'}}}
        with patch.object(market, 'http_get', return_value=json.dumps(payload).encode()):
            with self.assertRaisesRegex(ValueError, 'explicit_daily_basis_missing'):
                data._daily_pair('sh600000')

    def test_tencent_trims_only_excess_oldest_requested_window_and_preserves_hashes(self):
        import json
        dates = [(self.t - timedelta(days=i+1)).date().isoformat() for i in range(321)]
        values = [[day, '10', '10.1', '10.2', '9.9', '100'] for day in dates]
        for field in ('qfqday', 'day'):
            for raw_count in (320, 321):
                with self.subTest(field=field, raw_count=raw_count):
                    raw = list(reversed(values[:raw_count]))
                    payloads = [{'data': {'sh600000': {field: values}}}, {'data': {'sh600000': {'day': raw}}}]
                    with patch.object(data, 'now', return_value=self.t), patch.object(market, 'http_get', side_effect=[json.dumps(p).encode() for p in payloads]):
                        pair = data._daily_pair('sh600000')
                    self.assertEqual([r['date'] for r in pair['qfq']], sorted(dates[:320]))
                    self.assertEqual(pair['qfq'], pair['bfq'])
                    expected = {'requested': 320, 'returned_counts': {'qfq': 321, 'bfq': raw_count},
                                'excluded_leading_dates': {'qfq': [dates[-1]], 'bfq': [dates[-1]] if raw_count == 321 else []}}
                    self.assertEqual(pair['window'], expected)
                    self.assertEqual(pair['response_hashes'], {'qfq': data._hash(values), 'bfq': data._hash(raw)})
                    evidence = data._affine_evidence(pair)
                    self.assertEqual(len(evidence['days']), 320)
                    self.assertEqual(evidence['window'], expected)
                    self.assertEqual(evidence['response_hashes'], pair['response_hashes'])

    def test_requested_window_does_not_hide_internal_or_latest_missing_dates(self):
        import json
        values = [[(self.t-timedelta(days=i+1)).date().isoformat(), '10', '10.1', '10.2', '9.9', '100'] for i in range(321)]
        for missing_side in ('qfq', 'bfq'):
            for missing_index in (0, 100):
                with self.subTest(side=missing_side, missing_index=missing_index):
                    data._adjustments.clear()
                    rows = {mode: [r for i, r in enumerate(values) if mode != missing_side or i != missing_index] for mode in ('qfq', 'bfq')}
                    payloads = [{'data': {'sh600000': {'qfqday': rows['qfq']}}}, {'data': {'sh600000': {'day': rows['bfq']}}}]
                    with patch.object(data, 'now', return_value=self.t), patch.object(market, 'http_get', side_effect=[json.dumps(p).encode() for p in payloads]), \
                         patch.object(data, '_eastmoney_pair', side_effect=ValueError('test_fallback_unavailable')):
                        with self.assertRaisesRegex(data.AdjustmentError, 'completed_ohlc_dates_unverified'):
                            data.adjustment_factors('sh600000')

    def test_blocked_tencent_uses_verified_eastmoney_pair_without_relaxing_gate(self):
        pair = {'qfq': self.daily, 'bfq': self.daily, 'observed_at': self.t.isoformat(),
                'source': 'Eastmoney', 'basis': 'explicit_qfq'}
        with patch.object(data, 'MOCK', False), patch.object(data, 'now', return_value=self.t), \
             patch.object(data, '_daily_pair', side_effect=market.Blocked('test blocked')), \
             patch.object(data, '_eastmoney_pair', return_value=pair):
            result = data.fetch('sh600000', 'day')
        self.assertTrue(result['quality']['eligible'], result['quality'])
        self.assertIn('Eastmoney', result['source'])
        self.assertEqual(result['source_attempts'], [{'source': 'tencent', 'reason': 'blocked'}, {'source': 'eastmoney', 'reason': 'verified'}])
        self.assertEqual(result['adjustment_evidence']['source'], 'Eastmoney')

    def test_all_evidence_sources_failed_preserves_distinct_failure_causes(self):
        with patch.object(data, 'MOCK', False), patch.object(data, 'now', return_value=self.t), \
             patch.object(data, '_daily_pair', side_effect=market.Blocked('test blocked')), \
             patch.object(data, '_eastmoney_pair', side_effect=ValueError('explicit_daily_basis_missing')), \
             patch.object(market, '_kline_sina', return_value=self.daily):
            result = data.fetch('sh600000', 'day')
        self.assertFalse(result['quality']['eligible'])
        for error in ('adjustment_unverified', 'tencent_blocked', 'eastmoney_explicit_daily_basis_missing'):
            self.assertIn(error, result['quality']['reason'])

    def test_partial_affine_evidence_tries_full_verified_fallback(self):
        bad = copy.deepcopy(self.daily)
        bad[3].update(open=8, close=9, high=20, low=2)
        broken = {'qfq': bad, 'bfq': self.daily, 'observed_at': self.t.isoformat()}
        good = {'qfq': self.daily, 'bfq': self.daily, 'observed_at': self.t.isoformat(), 'source': 'Eastmoney'}
        with patch.object(data, 'MOCK', False), patch.object(data, 'now', return_value=self.t), \
             patch.object(data, '_daily_pair', return_value=broken), patch.object(data, '_eastmoney_pair', return_value=good):
            result = data.fetch('sh600000', 'day')
        self.assertTrue(result['quality']['eligible'], result['quality'])
        self.assertEqual(result['source_attempts'][0]['reason'], 'completed_ohlc_dates_unverified')

    def test_minute_cross_source_range_mismatch_still_blocks(self):
        ev = {'verified': True, 'observed_at': self.t.isoformat(), 'input_hash': 'test',
              'days': {day: {'a': 1, 'b': 0, 'raw_low': 10, 'raw_high': 10.2} for day in ('2026-11-05', '2026-11-06', '2026-11-09')}}
        bundle = {'evidence': ev, 'pair': {}, 'attempts': [{'source': 'eastmoney', 'reason': 'verified'}]}
        with patch.object(data, 'MOCK', False), patch.object(data, 'now', return_value=self.t), \
             patch.object(data, 'adjustment_factors', return_value=bundle), \
             patch.object(market, '_try_sources', return_value=(self.minute, 'Sina raw minute')):
            result = data.fetch('sh600000', '5m')
        self.assertFalse(result['quality']['eligible'])
        self.assertIn('minute_raw_daily_range_mismatch', result['quality']['reason'])

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
