import os,sys,unittest
from pathlib import Path
from unittest.mock import patch
os.environ['AQ5_DATA']=os.environ['AQ5_TEST_DATA']
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from aq5 import decision_collect, analysis, decision
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

if __name__=='__main__':unittest.main()
