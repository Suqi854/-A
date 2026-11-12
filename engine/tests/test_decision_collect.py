import os,sys,unittest
from pathlib import Path
os.environ['AQ5_DATA']=os.environ['AQ5_TEST_DATA']
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
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

if __name__=='__main__':unittest.main()
