import os,sys,tempfile,unittest,copy
from pathlib import Path
from datetime import datetime,timedelta
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
os.environ['AQ5_DATA']=tempfile.mkdtemp(prefix='forecast-legacy-tests-')
from aq5 import db,hf,sim
from aq5.config import CN_TZ,load_settings
from aq5.forecast import ledger,legacy

class LegacyTests(unittest.TestCase):
    def setUp(self):
        db.init();ledger.init();legacy.init()
        with db.tx() as c:
            for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'forecast_%'").fetchall():
                c.execute('DELETE FROM '+r[0])
        self.t=datetime(2026,11,9,10,tzinfo=CN_TZ)
        self.days=['2026-11-06','2026-11-09','2026-11-10']
        self.settings=load_settings();self.settings['strategy_v2']['legacy_auto']=True
        self.settings['pick']['ai_mode']='high'
        self.patchers=[patch.object(ledger,'official_days',return_value=self.days),patch.object(legacy,'official_days',return_value=self.days)]
        for p in self.patchers:p.start()
        ledger.initialize(100000,[],{},self.t)

    def tearDown(self):
        for p in self.patchers:p.stop()

    def quote(self,t,price=10):
        return {'price':price,'time':t.isoformat(),'received_at':t.isoformat(),'source':'test',
                'name':'test','limit_up':11.,'limit_down':9.,'pct':1.,'avg':9.8,'bids':[[10,1000]],'asks':[[10,100]]}

    def ctx(self,t=None,price=10,book=legacy.BOOK):
        t=t or self.t
        return legacy.Context({'sh600000':self.quote(t,price)},t,settings=self.settings,market_context={},book=book)

    def test_original_hf_policy_generates_isolated_order(self):
        global_state=copy.deepcopy(hf.STATE);global_buf=copy.deepcopy(hf._buf)
        with patch.object(hf,'_scan',{'t':self.t.timestamp(),'long':[{'code':'sh600000','name':'test','score':80}],'short':[]}):
            for i in range(16):
                ctx=self.ctx(self.t+timedelta(seconds=3*i),10+i*.01)
                hf.tick(ctx);ctx.save()
        orders=db.q('SELECT * FROM forecast_orders')
        self.assertEqual(len(orders),1)
        self.assertEqual(orders[0]['book'],'legacy_control')
        self.assertEqual(orders[0]['status'],'pending')
        self.assertTrue(orders[0]['reason'].startswith('高频·做多'))
        self.assertEqual(db.q('SELECT * FROM orders'),[])
        self.assertEqual(hf.STATE,global_state);self.assertEqual(hf._buf,global_buf)

    def test_t1_and_short_constraints(self):
        ctx=self.ctx();order=ctx.place('ai','sh600000','buy',100,reason='高频·test')
        self.assertEqual(order['status'],'pending')
        t=self.t+timedelta(seconds=3);ledger.match({'sh600000':self.quote(t)},t)
        ctx=self.ctx(t)
        self.assertEqual(ctx.available('ai','sh600000'),0)
        with self.assertRaises(ValueError):ctx.place('ai','sh600000','sell',100,reason='must block t1')
        self.assertFalse(ctx.ai_rules()['short'])

    def test_original_auto_follow_not_proxy(self):
        ctx=self.ctx()
        picks=[{'code':'sh600000','name':'test','price':10,'tier':'A','rank':1,'score':80,'signals':[],
                'plan':{'stop':9.5,'target1':10.5,'target2':11},'eligible':True}]
        sim.auto_follow(picks,'auction',context=ctx)
        orders=db.q('SELECT * FROM forecast_orders')
        self.assertEqual(len(orders),1)
        self.assertEqual(orders[0]['qty'],1100)
        self.assertIn('竞价选股[A级',orders[0]['reason'])
        self.assertEqual(db.q('SELECT * FROM orders'),[])

    def test_state_survives_restart_and_epoch_isolation(self):
        ctx=self.ctx();ctx.best['sh600000']=2.;ctx.cool['sh600000']=ctx.clock();ctx.save()
        self.assertEqual(self.ctx().best['sh600000'],2.)
        ledger.initialize_version(2,{},self.t)
        other=self.ctx(book='legacy_control@2')
        self.assertEqual(other.best,{})
        other.best['sh600000']=3.;other.save()
        self.assertEqual(self.ctx().best['sh600000'],2.)

    def test_shared_exit_policy_and_filled_stage(self):
        ctx=self.ctx();ctx.place('ai','sh600000','buy',200,reason='auction',stop=9.5,target1=10.2,target2=10.8);ctx.save()
        t=self.t+timedelta(seconds=3);ledger.match({'sh600000':self.quote(t)},t)
        tomorrow=self.t+timedelta(days=1)
        ctx=self.ctx(tomorrow,10.3)
        p=ctx.positions()['sh600000']
        qty,reason=sim.exit_decision(p,10.3,p['avail'],self.settings['risk'],tomorrow.date().isoformat())
        self.assertEqual(qty,100);self.assertIn('第一止盈',reason)
        ctx.risk_tick();ctx.save()
        self.assertEqual(ctx.positions()['sh600000']['stage'],0)
        later=tomorrow+timedelta(seconds=3);ledger.match({'sh600000':self.quote(later,10.3)},later)
        ctx=self.ctx(later,10.3);p=ctx.positions()['sh600000']
        self.assertEqual(p['stage'],1)
        self.assertGreaterEqual(p['stop'],10.)

    def test_market_context_uses_cache_and_fails_closed_without_network(self):
        ctx=self.ctx();ctx.backdrop=None
        with patch.object(legacy.marketstate,'state',side_effect=AssertionError('network forbidden')):
            with patch.object(legacy.marketstate,'_cache',{'t':ctx.clock(),'data':{'risk_off':False}}):
                self.assertFalse(ctx.market_state()['risk_off'])
            with patch.object(legacy.marketstate,'_cache',{'t':ctx.clock()-61,'data':{'risk_off':False}}):
                self.assertTrue(ctx.market_state()['risk_off'])
        self.assertTrue(db.q1("SELECT 1 FROM forecast_ledger_events WHERE kind='legacy_data_gap'"))

if __name__=='__main__':unittest.main()
