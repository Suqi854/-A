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
from aq5.forecast import service, chan, chan_review, data


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
        day = result['structures']['sz002911:day']
        self.assertTrue(day['structures']['strokes'])
        self.assertEqual(day['price_bars'][-1]['date'], day['source_time'])
        self.assertEqual(day['review']['close'], result['last_completed_close']['price'])
        expected = {'day':'15:00', '60m':'10:30', '15m':'09:45', '5m':'09:35'}
        for tf, hour in expected.items():
            frame = result['structures']['sz002911:' + tf]
            scenario = frame['scenario']
            self.assertEqual(scenario['status'], 'ready', tf)
            self.assertEqual(scenario['source_time'], frame['price_bars'][-1]['date'])
            self.assertEqual(scenario['base_close'], frame['price_bars'][-1]['close'])
            self.assertEqual(scenario['timeframe'], tf)
            self.assertEqual(scenario['due_at'], f'2026-11-16T{hour}:00+08:00')
            self.assertEqual(scenario['issued_at'], self.at.isoformat())
            self.assertTrue(scenario['reference_id'])
            self.assertNotIn('probability', scenario)
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
        self.assertEqual(result['structures']['sz002911:day']['review'], {'state': 'unavailable'})
        self.assertEqual(result['structures']['sz002911:day']['scenario']['status'], 'quality_blocked')

    def test_next_bar_deadlines_follow_exchange_sessions_and_require_current_inputs(self):
        days = ['2026-11-13', '2026-11-16']
        for tf, expected in [('5m', '13:05'), ('15m', '13:15'), ('60m', '14:00')]:
            self.assertEqual(data.next_bar_end(tf, '2026-11-13 11:30', days).isoformat(),
                             f'2026-11-13T{expected}:00+08:00')
        self.assertIsNone(data.next_bar_end('5m', '2026-11-13 11:32', days))
        self.assertIsNone(data.next_bar_end('day', '2026-11-16 15:00', days))

    def test_scenario_freezes_only_confirmed_prices_and_expires_at_next_close(self):
        days = ['2026-11-13']
        at = '2026-11-13 11:32'
        stroke = dict(start_price=10., end_price=12., direction='up', confirmed=True,
                      level='stroke', id='old', confirmed_at='2026-11-13T11:00:00+08:00')
        center = dict(low=10., high=11., confirmed=True, level='stroke', id='center', confirmed_at=stroke['confirmed_at'])
        result = dict(timeframe='5m', source_time='2026-11-13T11:30:00+08:00',
                      chart={'bars':[dict(close=11.5, low=10.8, high=11.7)]},
                      structures={'strokes':[stroke, {**stroke,'end_price':11.8,'confirmed':False}], 'segments':[]})
        review = dict(close=11.5,state='neutral',centers={'stroke':center},reference_layer='stroke')
        for movement, direction, expected in [('rising','up','up'), ('falling','down','down'),
                                               ('rising','down','range'), ('single','up','range')]:
            center['movement'] = movement
            stroke.update(direction=direction, end_price=12. if direction=='up' else 9.)
            projection = chan_review.next_bar_scenario(result, review, at, days)
            self.assertEqual(projection['primary'], expected)
            self.assertEqual(projection['reference_kind'], 'center')
            self.assertEqual(projection['checkpoint'], {'up':12.,'down':11.,'range':None}[expected])
        center['movement'] = 'rising'
        result['structures']['segments'] = [dict(level='segment', direction='down', confirmed=True)]
        self.assertEqual(chan_review.next_bar_scenario(result, review, at, days)['primary'], 'range')
        review.update(centers={},reference_layer=None)
        result['structures']['segments'][0]['direction'] = 'up'
        fallback = chan_review.next_bar_scenario(result, review, at, days)
        self.assertEqual((fallback['reference_kind'], fallback['high']), ('stroke', 12.))
        self.assertEqual(fallback['primary'], 'up')
        self.assertEqual(chan_review.next_bar_scenario(result, review, '2026-11-13 13:05', days)['status'], 'expired')
        self.assertEqual(chan_review.next_bar_scenario(result, review, at, [])['status'], 'calendar_unavailable')
        result['structures']['strokes'] = [dict(stroke,confirmed=False)]
        self.assertEqual(chan_review.next_bar_scenario(result, review, at, days)['status'], 'no_reference')

    def test_review_uses_comparable_centers_and_only_confirmed_divergence(self):
        def center(begin, end, low, high, level='stroke', confirmed=True):
            return dict(start=begin, end=end, low=low, high=high, level=level, confirmed=confirmed)
        def point(at, confirmed):
            return dict(at=at, level='stroke', side='buy', confirmed=confirmed, types=['1'])
        old, tentative = point('2026-11-04', True), point('2026-11-07', False)
        result = {'chart': {'bars': [{'close': 15}],
                            'divergence': [{**old, 'ratio': .7}, {**tentative, 'ratio': .6}]},
                  'structures': {'centers': [
                      center('2026-11-01','2026-11-02',9,10),
                      center('2026-11-02','2026-11-04',8,20),  # overlaps latest in time
                      center('2026-11-03','2026-11-05',11,13),
                      center('2026-11-04','2026-11-06',16,18,'segment'),
                      center('2026-11-06','2026-11-07',17,19,confirmed=False),
                  ], 'buy_sell_points': [old, tentative]}}
        review = chan_review.review(result, {'eligible': True})
        self.assertEqual(review['state'], 'up')
        self.assertEqual(review['reference_layer'], 'stroke')
        self.assertEqual(review['centers']['stroke']['previous']['low'], 9)
        self.assertEqual(review['centers']['segment']['movement'], 'single')
        self.assertEqual(review['latest_point'], old)
        self.assertEqual(review['divergence']['ratio'], .7)
        result['chart']['bars'][-1]['close'] = 13
        self.assertEqual(chan_review.review(result, {'eligible': True})['state'], 'neutral')

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
