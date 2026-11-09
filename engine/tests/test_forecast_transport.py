import os,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
os.environ['AQ5_DATA']=tempfile.mkdtemp(dir=os.environ['AQ5_DATA'])
os.environ['AQ5_MOCK']='1'
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from aq5 import live,picker


class ForecastTransport(unittest.TestCase):
    def test_source_annotated_quote_obeys_deployed_worker_allowlist(self):
        code='sh600000'
        q={'code':code,'price':10.,'time':'20261109143000','source':'Tencent',
           'received_at':'2026-11-09T14:30:01.123+08:00','time_basis':'tencent_quote_timestamp',
           'qty':100,'cost':9,'token':'synthetic-private'}
        with patch.object(picker,'runs',return_value=[{'items':[{'code':code}]}]),patch.object(live,'load_settings',return_value={'indices':[code]}):
            out=live.public_snapshot({code:q})
        allowed={'code','name','price','pct','time','chg','amount'}
        self.assertEqual(set(out['pick_quotes'][code]),allowed)
        self.assertEqual(set(out['indices'][0]),allowed)
        self.assertNotIn('synthetic-private',str(out))
        self.assertNotIn('received_at',str(out))

if __name__=='__main__':unittest.main()
