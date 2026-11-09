import os,sys,tempfile,unittest,math
from pathlib import Path
from datetime import datetime,timedelta
os.environ['AQ5_DATA']=tempfile.mkdtemp(dir=os.environ['AQ5_DATA'])
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from aq5.forecast import features
from aq5.config import CN_TZ


class FeaturesTest(unittest.TestCase):
    def setUp(self):
        self.t=datetime(2026,11,9,10,tzinfo=CN_TZ)
        self.rows=[]
        for i in range(150):
            p=10+i*.01+.2*math.sin(i/5)
            self.rows.append({'date':(self.t-timedelta(minutes=5*(150-i))).isoformat(),'available_at':self.t.isoformat(),
                              'open':p,'close':p+.01,'high':p+.1,'low':p-.1,'vol':100+i})

    def test_measured_families_and_missing_sources(self):
        r=features.build(self.rows,asof=self.t)
        self.assertTrue(all(math.isfinite(v) for v in r['values'].values()))
        self.assertGreater(len(r['values']),50)
        self.assertEqual(r['values']['missing_fundamental'],1)
        self.assertNotIn('flow_main',r['values'])
        self.assertEqual(next(c for c in r['coverage'] if c['id']=='unimplemented')['status'],'unavailable')

    def test_future_bars_and_disclosure_not_used(self):
        a=features.build(self.rows,asof=self.t)
        b=features.build(self.rows+[dict(self.rows[-1],date=(self.t+timedelta(minutes=5)).isoformat())],asof=self.t,
                         disclosure={'available_at':(self.t+timedelta(seconds=1)).isoformat(),'source':'official','values':{'roe':.2}})
        self.assertEqual(a['values'],b['values'])
        self.assertEqual(b['excluded'],1)

    def test_prices_normalized_and_duplicates_rejected(self):
        a=features.build(self.rows,asof=self.t)['values']
        doubled=[{**r,**{k:r[k]*2 for k in ('open','high','low','close')}} for r in self.rows]
        b=features.build(doubled,asof=self.t)['values']
        for k in ('trend_ma20_distance','momentum_return5','risk_atr','volume_cmf20'):
            self.assertAlmostEqual(a[k],b[k])
        with self.assertRaises(ValueError):
            features.build(self.rows+[self.rows[-1]],asof=self.t)

    def test_quote_needs_source_time_and_known_time(self):
        q={'time':self.t.isoformat(),'source':'fixture','pe':10,'pb':1,'bids':[[10,100]],'asks':[[10.01,200]]}
        missing=features.build(self.rows,asof=self.t,quote=q)
        self.assertEqual(missing['values']['missing_valuation'],1)
        q['received_at']=self.t.isoformat()
        valid=features.build(self.rows,asof=self.t,quote=q)
        self.assertEqual(valid['values']['valuation_pe'],10)
        self.assertAlmostEqual(valid['values']['microstructure_imbalance'],-1/3)
        q.pop('time')
        received_only=features.build(self.rows,asof=self.t,quote=q)
        self.assertEqual(received_only['values']['missing_valuation'],1)

    def test_daily_flow_is_not_mislabeled_as_intraday_flow(self):
        ctx={'flow':{'source':'daily','source_time':'2026-11-06','source_precision':'date','granularity':'day',
                     'known_at':self.t.isoformat(),'values':{'main_last_yuan':123}}}
        result=features.build(self.rows,asof=self.t,context=ctx)
        self.assertEqual(result['values']['flow_main_last_yuan'],123)
        ctx['flow']['source_time']='2026-11-05'
        result=features.build(self.rows,asof=self.t,context=ctx)
        self.assertEqual(result['values']['missing_flow'],1)

    def test_future_context_and_source_after_known_time_are_rejected(self):
        ctx={'market':{'source':'fixture','source_time':self.t.isoformat(),
                       'known_at':(self.t-timedelta(seconds=1)).isoformat(),'values':{'index_pct':1}}}
        result=features.build(self.rows,asof=self.t,context=ctx)
        self.assertNotIn('market_index_pct',result['values'])
        ctx['market']['known_at']=(self.t+timedelta(seconds=1)).isoformat()
        result=features.build(self.rows,asof=self.t,context=ctx)
        self.assertNotIn('market_index_pct',result['values'])

    def test_equivalent_timezone_bars_are_duplicates(self):
        from datetime import timezone
        duplicate=dict(self.rows[-1],date=features.stamp(self.rows[-1]['date']).astimezone(timezone.utc).isoformat())
        with self.assertRaises(ValueError):
            features.build(self.rows+[duplicate],asof=self.t)

    def test_core_reusable_distribution_metrics_are_present(self):
        result=features.build(self.rows,asof=self.t)['values']
        for key in ('risk_sharpe60','risk_mdd60','risk_boll_pctile','math_reg_sd','math_ar1'):
            self.assertTrue(math.isfinite(result[key]),key)

if __name__=='__main__':unittest.main()
