import io,json,os,sys,unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
os.environ['AQ5_DATA']=os.environ['AQ5_TEST_DATA']
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from aq5 import decision_collect, analysis, decision, marketstate
from aq5.config import CN_TZ
from aq5.decision_collect import _lhb_details

class DisclosureTests(unittest.TestCase):
    def test_one_sided_disclosure_never_formats_missing_amount_as_zero(self):
        text='\n'.join(_lhb_details([{'trade_date':'2026-11-04','non_institution_net':None,
            'seats':[{'name':'样例营业部','category':'brokerage_seat_proxy','buy':15997721,'sell':None,'net':None}]}]))
        self.assertIn('买入1599.77万',text);self.assertIn('卖出未披露',text);self.assertIn('净额未披露',text)
        self.assertNotIn('+0.00',text)
    def test_complete_net_and_no_list_keep_existing_meanings(self):
        self.assertIn('净额 +0.50亿元','\n'.join(_lhb_details([{'trade_date':'2026-11-04','non_institution_net':50_000_000,'seats':[]}])) )
        self.assertIn('不推断没有游资参与',_lhb_details([])[0])

    def test_full_report_reuses_one_structure_read_for_plan_and_chart(self):
        code = 'sh600000'
        forecast = {'symbol': {'code': code}, 'summary': {}, 'structures': {code + ':day': {}},
                    'predictions': [], 'request': {}, 'status': {}, 'coverage': []}
        with (patch.object(decision_collect.db, 'init'),
              patch.object(decision_collect.market, 'snapshot', return_value=[]),
              patch.object(decision_collect.market, 'kline_with_live', return_value=[]),
              patch.object(decision_collect.evidence, 'latest', return_value={}),
              patch.object(decision_collect, 'collect', return_value={'sources': {}}),
              patch.object(analysis, 'analyze', return_value={'code': code}),
              patch.object(decision, 'load_forecast', return_value=forecast) as load,
              patch.object(decision, 'view', return_value={}) as view,
              patch.object(decision, 'merge', side_effect=lambda raw, fusion: raw)):
            report = decision_collect.analysis(code)
        load.assert_called_once_with(code)
        view.assert_called_once_with(code, forecast=forecast)
        self.assertEqual(report['forecast']['structures'], forecast['structures'])

    def test_cloud_report_uses_a_targeted_quote_and_reuses_technical_candles(self):
        code = 'sh600522'
        origin = 'https://public.example'
        quote = {'code': code, 'name': '中天科技', 'price': 22., 'amount': 1000.}
        kline = [{'date': '2026-11-17', 'close': 22.}]
        structures = {code + ':' + tf: {} for tf in ('day', '60m', '15m', '5m')}
        forecast = {'symbol': {'code': code}, 'summary': {}, 'structures': structures,
                    'predictions': [], 'request': {}, 'status': {}, 'coverage': []}
        with (patch.object(decision_collect.db, 'init'),
              patch.object(decision_collect.market, 'snapshot', side_effect=AssertionError('full market read')),
              patch.object(decision_collect.market, 'quotes', return_value={code: quote}) as quotes,
              patch.object(decision_collect.market, 'kline_with_live', side_effect=AssertionError('duplicate K-line load')),
              patch.object(decision_collect.evidence, 'latest', return_value={}),
              patch.object(decision_collect, '_published_breadth', return_value={}),
              patch.object(decision_collect, 'collect', return_value={'sources': {}}) as collect,
              patch.object(analysis, 'analyze', return_value={'code': code, 'kline': kline}) as technical,
              patch.object(decision, 'load_forecast', return_value=forecast),
              patch.object(decision, 'view', return_value={}),
              patch.object(decision, 'merge', side_effect=lambda raw, fusion: raw)):
            report = decision_collect.analysis(code, public_origin=origin)
        quotes.assert_called_once_with([code])
        technical.assert_called_once_with(code, published_breadth={})
        self.assertEqual(collect.call_args.args, ([quote], [code]))
        self.assertEqual(report['kline'], kline)
        self.assertEqual(report['forecast']['structures'], structures)

    def test_public_breadth_accepts_current_or_final_data_with_real_coverage(self):
        at = datetime(2026, 11, 17, 21, 0, tzinfo=CN_TZ)
        payload = {'time': '2026-11-17 15:06:00', 'phase': 'closed', 'mock': False,
                   'breadth': {'total': 3200, 'up': 1600, 'median_pct': -0.3}}
        cases = [(payload, True), ({**payload, 'time': '2026-11-17 20:59:40'}, True),
                 ({**payload, 'time': '2026-11-17 14:55:00', 'phase': 'trading'}, False),
                 ({**payload, 'time': '2026-11-17 21:01:00'}, False),
                 ({**payload, 'mock': True}, False), ({**payload, 'breadth': {'total': 6}}, False)]
        for value, expected in cases:
            with self.subTest(time=value['time'], expected=expected):
                with patch.object(decision_collect, 'urlopen', return_value=io.BytesIO(json.dumps(value).encode())):
                    result = decision_collect._published_breadth('https://public.example', at)
                self.assertEqual(bool(result), expected)

    def test_stock_market_state_uses_the_main_index_and_does_not_write_alerts(self):
        with (patch.object(marketstate, 'index_state', return_value={'code': 'sh000300', 'pct': 1, 'live': True}) as index,
              patch.object(marketstate, '_alert_indices', side_effect=AssertionError('alerts')),
              patch.object(marketstate, '_track_shift', side_effect=AssertionError('shifts')),
              patch.object(marketstate.market, 'snapshot', side_effect=AssertionError('full market read'))):
            result = marketstate.state(breadth={'total': 3000, 'up': 2100})
        index.assert_called_once_with('sh000300', '沪深300')
        self.assertEqual(result['temperature'], 70.0)

if __name__=='__main__':unittest.main()
