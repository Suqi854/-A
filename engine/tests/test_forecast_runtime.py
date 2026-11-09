"""Research/execution integration against the real append-only forecast ledger."""
import os
import queue
import sys
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ['AQ5_DATA'] = tempfile.mkdtemp(prefix='forecast-runtime-', dir=os.environ['AQ5_DATA'])
os.environ['AQ5_MOCK'] = '0'
from aq5 import db
from aq5.config import CN_TZ
from aq5.forecast import ledger, models, evaluation, runtime


class ForecastRuntimeTests(unittest.TestCase):
    code = 'sh600000'
    t = datetime(2026,11,9,10,tzinfo=CN_TZ)

    def setUp(self):
        db.init(); ledger.init(); models.init(); evaluation.init(); runtime.init()
        with db.tx() as c:
            for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'forecast_%'").fetchall():
                c.execute('DELETE FROM "'+r[0]+'"')
            for table in ('accounts','positions','orders','trades'):
                c.execute('DELETE FROM '+table)
        runtime._quotes.clear();runtime._cache.clear();runtime._due.clear();runtime._stop.clear()
        runtime.STATE.update(phase='not_started',error=None,code_status={})

    def q(self,t=None,price=10.):
        t=t or self.t
        return {'price':price,'time':t.isoformat(),'source':'fixture','received_at':t.isoformat(),
                'limit_up':11.,'limit_down':9.}

    def structure(self,code,tf,t,eligible=True,confirmed=True):
        bar_time=(t-timedelta(minutes=5)).replace(second=0,microsecond=0)
        bars={'rows':[{'date':bar_time.isoformat(),'open':10.,'close':10.,'high':10.1,'low':9.9,'vol':100}],
              'quality':{'eligible':eligible,'reason':None if eligible else 'gap'},'source':'fixture'}
        structure={'id':'s-'+tf,'code':code,'timeframe':tf,'asof':t.isoformat(),
                   'quality':{'eligible':eligible},'confirmed':confirmed,
                   'features':{'chan_segment_direction':1.},'structures':{'centers':[]}}
        return bars,structure

    def model(self,*args,**kwargs):
        return {'probability':.7,'prior_probability':.5,'quantiles':{'p10':-.01,'p50':.02,'p90':.03},
                'model_version':kwargs.get('model_version',1),'validated':True,'status':'candidate'}

    def seed_model(self,version,status):
        with db.tx() as c:
            c.execute('INSERT INTO forecast_models(version,created,week,status,training_hash,artifact,metrics) VALUES(?,?,?,?,?,?,?)',
                      (version,'2026-11-06T15:30:00+08:00','week-'+str(version),status,'fixture','{}','{}'))

    def research(self,*,eligible=True,confirmed=True,synthetic=False):
        stack=ExitStack()
        stack.enter_context(patch.object(runtime,'_structure',side_effect=lambda code,tf,t:self.structure(code,tf,t,eligible,confirmed)))
        stack.enter_context(patch.object(runtime.features,'build',return_value={
            'values':{'trend_20':.1,'chan_5m_buy_point':1.,'chan_5m_sell_point':0.,
                      'chan_5m_point_age_bars':1.,'chan_5m_point_confirmation_age_bars':1.},
            'coverage':[],'hash':'features','version':'test'}))
        stack.enter_context(patch.object(runtime.data,'context',return_value={}))
        stack.enter_context(patch.object(runtime.data,'disclosure',return_value={}))
        stack.enter_context(patch.object(runtime.models,'predict',side_effect=self.model))
        stack.enter_context(patch.object(runtime,'MOCK',synthetic))
        return stack

    def book(self,cash=10000,qty=300):
        return {'cash':cash,'baseline':{'equity_fen':cash*100+qty*1000},
                'holdings':{self.code:{'qty':qty,'lots':[{'qty':qty,'acquired_at':'2026-11-06T10:00:00+08:00'}]}} if qty else {},
                'open_cycles':[]}

    def prediction(self,id,horizon='30m',t=None,**extra):
        t=t or self.t
        r={'id':id,'code':self.code,'horizon':horizon,'asof':t.isoformat(),
           'due_at':(t+timedelta(days=3) if horizon.endswith('d') else t+timedelta(minutes=30)).isoformat(),
           'reference_price':10.,'source_time':t.isoformat(),'features':{},'variant':'fusion','model_version':1,
           'origin':'forward','probability':.7,'prior_probability':.5,'validated':True,
           'quantiles':{'p10':-.01,'p50':.02,'p90':.03},'quality':{'eligible':True},
           'alignment':{'eligible':True,'state':'aligned','direction':1},'invalidation':9.2,**extra}
        ledger.record_prediction(r)
        return r

    def test_sampling_prediction_settlement_training_pipeline(self):
        runtime._quotes[self.code]=self.q()
        with self.research():
            self.assertEqual(runtime.process_code(self.code,self.t)['issued'],18)
            self.assertEqual(runtime.process_code(self.code,self.t+timedelta(seconds=2))['issued'],0)
        due=self.t+timedelta(minutes=30)
        self.assertEqual(ledger.settle({self.code:self.q(due,10.2)},due)['settled'],3)
        rows=ledger.training_rows()
        self.assertEqual({r['horizon'] for r in rows},{'30m'})
        self.assertEqual({r['variant'] for r in rows},{'fusion'})
        self.assertEqual(len(rows),1)  # Same input/label is not tripled by feature ablations.
        self.assertTrue(all(abs(r['outcome_return']-.02)<1e-9 for r in rows))
        with patch.object(models,'train_candidate',return_value={'trained':False}) as trainer:
            runtime.maintenance(self.t.replace(hour=15,minute=15))
            self.assertEqual(len(trainer.call_args.args[0]),1)

    def test_no_fresh_quote_emits_no_forecast(self):
        runtime._quotes[self.code]=self.q(self.t-timedelta(seconds=13))
        with self.research():
            result=runtime.process_code(self.code,self.t)
        self.assertEqual(result['issued'],0)
        self.assertEqual(result['failures']['quote'],'missing_or_stale_quote')

    def test_unknown_calendar_year_is_closed(self):
        t=self.t.replace(year=2027)
        runtime._quotes[self.code]=self.q(t)
        with self.research():
            self.assertEqual(runtime.process_code(self.code,t)['issued'],0)

    def test_synthetic_run_never_becomes_forward(self):
        runtime._quotes[self.code]=self.q()
        with self.research(synthetic=True):runtime.process_code(self.code,self.t)
        self.assertEqual({r['origin'] for r in ledger.predictions()}, {'synthetic'})
        ledger.initialize(10000,[],{},self.t)
        r=ledger.predictions()[0]
        result=ledger.submit(r['variant'],self.code,'buy',100,r['id'],self.q(),self.t)
        self.assertEqual(result['status'],'rejected')

    def test_missing_model_prevents_math_orders(self):
        p=self.prediction('long','3d',validated=False,probability=None,
                          quantiles={'p10':None,'p50':None,'p90':None})
        short=self.prediction('short',validated=False,probability=None,
                              quantiles={'p10':None,'p50':None,'p90':None})
        action,qty,why,_=runtime.decision(p,short,self.book(qty=0),self.code,self.q(),self.t)
        self.assertEqual((action,qty,why),('wait',0,'model_unvalidated'))

    def test_opening_clones_plan_without_altering_original(self):
        with db.tx() as c:
            c.execute("INSERT INTO accounts(id,name,cash,init_cash,created,peak) VALUES('plan','plan',5000,8000,?,8000)",(self.t.isoformat(),))
            c.execute("INSERT INTO positions(account,code,name,qty,cost,last_buy) VALUES('plan',?,'fixture',300,8,?)",
                      (self.code,'2026-11-06T10:00:00+08:00'))
        self.assertTrue(runtime._opening({self.code:self.q()},self.t))
        for b in ledger.view()['books'].values():
            self.assertEqual(b['cash'],5000)
            self.assertEqual(b['holdings'][self.code]['qty'],300)
            self.assertEqual(ledger._available(b['holdings'][self.code],self.t),300)
        self.assertEqual(db.q1("SELECT cash FROM accounts WHERE id='plan'")['cash'],5000)

    def test_short_or_margin_opening_is_not_silently_dropped(self):
        with db.tx() as c:
            c.execute("INSERT INTO accounts(id,name,cash,init_cash,created,peak) VALUES('ai','ai',-1,100,?,100)",(self.t.isoformat(),))
        self.assertFalse(runtime._opening({},self.t))
        self.assertEqual(ledger.view()['status'],'unconfigured')

    def test_pause_does_not_trade_or_touch_original_accounts(self):
        ledger.initialize(10000,[],{},self.t)
        evaluation.control('pause',self.t)
        with (patch.object(runtime,'_paper_decisions') as paper,patch.object(runtime,'_ai_decisions') as ai,
              patch.object(ledger,'match',wraps=ledger.match) as match):
            runtime.execute({self.code:self.q()},self.t)
        paper.assert_not_called();ai.assert_not_called();match.assert_not_called()
        self.assertEqual(runtime.STATE['phase'],'paused')
        self.assertEqual(db.q1('SELECT COUNT(*) AS n FROM orders')['n'],0)

    def test_current_chan_feature_names_trigger_rule_entry(self):
        long=self.prediction('long','3d')
        short=self.prediction('short',features={'chan_5m_buy_point':1.,'chan_5m_sell_point':0.,
                                              'chan_5m_point_age_bars':10.,'chan_5m_point_confirmation_age_bars':1.})
        action,qty,_,_=runtime.decision(long,short,self.book(qty=0),self.code,self.q(),self.t,rule=True)
        self.assertEqual(action,'buy')
        self.assertGreaterEqual(qty,100)

    def test_unconfirmed_structure_is_not_tradable(self):
        runtime._quotes[self.code]=self.q()
        with self.research(confirmed=False):runtime.process_code(self.code,self.t)
        self.assertTrue(ledger.predictions())
        self.assertTrue(all(not r['quality']['eligible'] for r in ledger.predictions()))

    def test_cross_horizon_conflict_waits_but_risk_exit_still_first(self):
        long=self.prediction('long','3d')
        short=self.prediction('short')
        short['alignment']['direction']=-1
        action,qty,reason,_=runtime.decision(long,short,self.book(qty=0),self.code,self.q(),self.t)
        self.assertEqual((action,reason),('wait','long_short_conflict'))
        long['invalidation']=11
        action,qty,reason,_=runtime.decision(long,short,self.book(qty=1000),self.code,self.q(),self.t)
        self.assertEqual((action,reason),('sell','long_risk_exit'))

    def test_training_sampling_never_replaces_missing_first_origin(self):
        first=self.prediction('first')
        later=self.prediction('later',t=self.t+timedelta(minutes=5))
        pm=self.prediction('pm',t=self.t.replace(hour=13))
        due=self.t+timedelta(minutes=35)
        ledger.settle({self.code:self.q(due,10.2)},due)
        due=self.t.replace(hour=13,minute=30)
        ledger.settle({self.code:self.q(due,10.2)},due)
        self.assertEqual([r['id'] for r in ledger.training_rows()],['pm'])
        self.assertEqual(len(ledger.predictions()),3)

    def test_latest_horizon_lookup_survives_many_short_predictions(self):
        ledger.initialize(10000,[],{},self.t)
        self.prediction('long','3d',model_version=0)
        for i in range(1501):
            self.prediction('s'+str(i),t=self.t+timedelta(seconds=i+1),model_version=0)
        t=self.t+timedelta(seconds=1502)
        with patch.object(runtime,'decision',return_value=('wait',0,'test','position')) as decide:
            runtime._paper_decisions({self.code:self.q(t)},t)
        # fusion is the only configured variant with predictions in this fixture.
        fusion_calls=[call for call in decide.call_args_list if call.args[0] and call.args[0]['variant']=='fusion']
        self.assertTrue(fusion_calls,'latest 3d forecast was lost behind the global short-forecast limit')
        self.assertEqual(fusion_calls[0].args[0]['id'],'long')

    def test_candidate_does_not_starve_active_predictions(self):
        self.seed_model(1,'active');self.seed_model(2,'candidate')
        runtime._quotes[self.code]=self.q()
        with self.research():
            self.assertEqual(runtime.process_code(self.code,self.t)['issued'],36)
        records=ledger.predictions()
        self.assertEqual({r['model_version'] for r in records},{1,2})
        self.assertEqual(len([r for r in records if r['model_version']==1]),18)

    def test_long_risk_exit_uses_unexpired_long_prediction(self):
        self.seed_model(1,'candidate')
        ledger.initialize(10000,[{'code':self.code,'qty':300,'acquired_at':'2026-11-06T10:00:00+08:00'}],
                          {self.code:self.q()},self.t)
        ledger.initialize_version(1,{self.code:self.q()},self.t)
        self.prediction('long','3d',invalidation=9.2)
        self.prediction('short',t=self.t-timedelta(minutes=31))
        runtime._paper_decisions({self.code:self.q(price=9.1)},self.t)
        order=db.q1("SELECT * FROM forecast_orders WHERE book='fusion@1'")
        self.assertIsNotNone(order)
        self.assertEqual(order['prediction_id'],'long')
        self.assertEqual(order['status'],'pending')

    def test_old_version_ai_intent_cannot_execute_after_switch(self):
        self.seed_model(2,'active')
        with db.tx() as c:
            c.execute("INSERT INTO forecast_controls VALUES('active_version','2')")
            c.execute("INSERT INTO accounts(id,name,cash,init_cash,created,peak) VALUES('ai','ai',10000,10000,?,10000)",(self.t.isoformat(),))
            prior=self.t-timedelta(seconds=3)
            c.execute('INSERT INTO forecast_ai_orders VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL)',
                      ('old-intent',self.code,'buy',100,prior.isoformat(),prior.isoformat(),'fixture',
                       (self.t+timedelta(minutes=5)).isoformat(),1,'pending',db.dumps({'zone':[9.5,10.5],'reason':'test'})))
        from aq5 import sim
        with patch.object(sim,'place',return_value={'id':99}) as place:
            runtime._ai_decisions({self.code:self.q()},self.t)
        place.assert_not_called()

    def test_feed_preserves_fractional_asof_and_event_snapshot(self):
        events=queue.Queue()
        first=self.t.replace(microsecond=900000)
        second=self.t+timedelta(seconds=1,microseconds=100000)
        with patch.object(runtime,'_started',True),patch.object(runtime,'_queue',events):
            runtime.feed({self.code:self.q(first,10.)},first)
            runtime.feed({self.code:self.q(second,10.1)},second)
        older,older_time=events.get_nowait()
        newer,newer_time=events.get_nowait()
        self.assertEqual(older_time,first.isoformat())
        self.assertEqual(newer_time,second.isoformat())
        self.assertEqual(older[self.code]['price'],10.)
        self.assertEqual(newer[self.code]['price'],10.1)


if __name__=='__main__':
    unittest.main()
