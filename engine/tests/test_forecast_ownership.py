"""Cross-module ownership regressions; synthetic evidence is not a promotion proof."""
import os,sys,tempfile,unittest,io
from pathlib import Path
from datetime import datetime,timedelta
from unittest.mock import patch
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
os.environ['AQ5_DATA']=tempfile.mkdtemp(prefix='forecast-ownership-tests-')
from aq5 import db,sim,market
from aq5.config import CN_TZ
from aq5.forecast import runtime,ledger,models,evaluation

class OwnershipTests(unittest.TestCase):
    def setUp(self):
        db.init();ledger.init();models.init();evaluation.init();runtime.init();sim.ensure_accounts()
        with db.tx() as c:
            for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'forecast_%'").fetchall():
                c.execute('DELETE FROM '+r[0])
            for table in ('positions','orders','trades'):c.execute('DELETE FROM '+table)
            c.execute("UPDATE accounts SET cash=1000000 WHERE id='ai'")
            c.execute("INSERT INTO forecast_models(version,created,week,status,training_hash,artifact,metrics) VALUES(1,'2026-01-01','2026-01','active','synthetic-test','{}','{}')")
            c.execute("INSERT INTO forecast_controls VALUES('active_version','1')")
        self.t=datetime(2026,11,9,10,tzinfo=CN_TZ);self.current=self.t;self.quotes={}
        self.patchers=[patch.object(sim,'now',side_effect=lambda:self.current),
                       patch.object(sim,'today',side_effect=lambda:self.current.date().isoformat()),
                       patch.object(sim,'can_trade_now',return_value=True),
                       patch.object(market,'quotes_recent',side_effect=lambda codes:{c:self.quotes[c] for c in codes if c in self.quotes})]
        for p in self.patchers:p.start()

    def tearDown(self):
        for p in self.patchers:p.stop()

    def quote(self,code='sh600000',price=10):
        q={'code':code,'name':code,'price':price,'time':self.current.isoformat(),
           'received_at':self.current.isoformat(),'source':'synthetic-test','limit_up':11,'limit_down':8,
           'up_limit':11,'dn_limit':8}
        self.quotes[code]=q;return q

    def forecast(self,code,horizon,at=None,eligible=True,probability=.8):
        at=at or self.t;due=at+timedelta(days=3) if horizon=='3d' else at+timedelta(minutes=30)
        record={'id':f'{code}-{horizon}-{at.isoformat()}','code':code,'horizon':horizon,'asof':at.isoformat(),
                'due_at':due.isoformat(),'source_time':at.isoformat(),'reference_price':10,'variant':'fusion',
                'origin':'forward','model_version':1,'probability':probability,'quantiles':{'p10':-.01,'p50':.02,'p90':.04},
                'quality':{'eligible':eligible},'validated':True,'invalidation':9.5,
                'alignment':{'state':'aligned','direction':1},'features':{}}
        ledger.record_prediction(record)

    def setup_predictions(self,code='sh600000'):
        for horizon in ('3d','30m'):self.forecast(code,horizon)

    def test_invalidated_data_cancels_pending_buy_before_execution(self):
        self.setup_predictions();q=self.quote();runtime._ai_decisions({'sh600000':q},self.current)
        self.assertEqual(len(db.q("SELECT * FROM forecast_ai_orders WHERE status='pending'")),1)
        self.forecast('sh600000','3d',self.t+timedelta(seconds=1),eligible=False)
        self.current=self.t+timedelta(seconds=3);q=self.quote()
        runtime._ai_decisions({'sh600000':q},self.current)
        self.assertEqual(db.q("SELECT * FROM trades WHERE account='ai'"),[],
                         'A fresh quote must not fill an intent after required forecast data became invalid')

    def test_pending_entries_reserve_position_slots(self):
        for code in ('sh600000','sh600001'):self.setup_predictions(code);self.quote(code)
        config={**runtime.cfg(),'max_positions':1}
        with patch.object(runtime,'cfg',return_value=config):
            runtime._ai_decisions(dict(self.quotes),self.current)
            self.current+=timedelta(seconds=3)
            for code in list(self.quotes):self.quote(code)
            runtime._ai_decisions(dict(self.quotes),self.current)
        self.assertLessEqual(len(db.q("SELECT * FROM positions WHERE account='ai'")),1,
                             'Parallel pending entries must obey the total position cap')

    def test_previous_inventory_remains_sellable_after_today_topup(self):
        self.setup_predictions()
        with db.tx() as c:
            c.execute("INSERT INTO positions(account,code,name,qty,cost,last_buy,opened,auto,stage) VALUES('ai','sh600000','test',1000,10,?, '2026-11-06',1,0)",(self.t.isoformat(),))
            c.execute("INSERT INTO trades(account,code,side,qty,time) VALUES('ai','sh600000','buy',100,?)",(self.t.strftime('%Y-%m-%d %H:%M:%S'),))
        self.assertEqual(sim.available('ai','sh600000'),900)
        q=self.quote(price=9.4)
        runtime._ai_decisions({'sh600000':q},self.current)
        sell=db.q1("SELECT qty FROM forecast_ai_orders WHERE code='sh600000' AND side='sell'")
        self.assertIsNotNone(sell,'A same-day topup must not lock yesterday inventory for risk exits')
        self.assertEqual(sell['qty'],900)

    def test_later_quote_fill_is_idempotent_and_cash_only(self):
        self.setup_predictions();q=self.quote();runtime._ai_decisions({'sh600000':q},self.current)
        self.assertEqual(db.q("SELECT * FROM trades WHERE account='ai'"),[])
        self.current+=timedelta(seconds=3);q=self.quote()
        for _ in range(2):runtime._ai_decisions({'sh600000':q},self.current)
        self.assertEqual(len(db.q("SELECT * FROM trades WHERE account='ai'")),1)
        self.assertGreaterEqual(db.q1("SELECT cash FROM accounts WHERE id='ai'")['cash'],0)
        self.assertEqual(sim.available('ai','sh600000'),0)

    def test_adopted_t_sell_has_durable_buyback_obligation(self):
        self.forecast('sh600000','3d')
        self.forecast('sh600000','30m',probability=.2)
        with db.tx() as c:
            c.execute("INSERT INTO positions(account,code,name,qty,cost,last_buy,opened,auto,stage) VALUES('ai','sh600000','test',1000,10,'2026-11-06','2026-11-06',1,0)")
        q=self.quote();runtime._ai_decisions({'sh600000':q},self.current)
        self.current+=timedelta(seconds=3);q=self.quote();runtime._ai_decisions({'sh600000':q},self.current)
        self.assertEqual(db.q1("SELECT qty FROM positions WHERE account='ai'")['qty'],700)
        self.current=self.t+timedelta(minutes=1)
        self.forecast('sh600000','30m',at=self.current,probability=.8)
        q=self.quote();runtime._ai_decisions({'sh600000':q},self.current)
        buy=db.q1("SELECT qty FROM forecast_ai_orders WHERE side='buy' AND status='pending'")
        self.assertIsNotNone(buy,'Adopted AI must track an open T sale and restore its exact inventory')
        self.assertEqual(buy['qty'],300)

    def test_adopted_entry_keeps_frozen_risk_exit_when_research_pauses(self):
        self.setup_predictions();q=self.quote();runtime._ai_decisions({'sh600000':q},self.current)
        self.current+=timedelta(seconds=3);q=self.quote();runtime._ai_decisions({'sh600000':q},self.current)
        position=db.q1("SELECT stop FROM positions WHERE account='ai' AND code='sh600000'")
        self.assertEqual(position['stop'],9.5,'Existing risk exit needs a durable boundary independent of future forecast availability')

    def test_restart_after_place_before_intent_commit_does_not_duplicate(self):
        self.setup_predictions();q=self.quote();runtime._ai_decisions({'sh600000':q},self.current)
        self.current+=timedelta(seconds=3);q=self.quote();runtime._ai_decisions({'sh600000':q},self.current)
        with db.tx() as c:c.execute("UPDATE forecast_ai_orders SET status='pending',order_id=NULL")
        self.current+=timedelta(seconds=3);q=self.quote();runtime._ai_decisions({'sh600000':q},self.current)
        self.assertEqual(len(db.q("SELECT * FROM trades WHERE account='ai'")),1)
        self.assertEqual(db.q1('SELECT status FROM forecast_ai_orders')['status'],'submitted')

    def test_forecast_routes_require_authentication(self):
        from aq5.server import Handler
        for path in ('/api/forecast','/api/forecast/evaluation','/api/forecast/models'):
            responses=[]
            request=SimpleNamespace(path=path,_authorized=lambda:False,
                _json=lambda body,status=200:responses.append(status),
                _handle=lambda *args:self.fail('unauthenticated request reached private data'))
            Handler.do_GET(request)
            self.assertEqual(responses,[401])
        responses=[]
        request=SimpleNamespace(path='/api/forecast/control',client_address=('198.51.100.1',123),
            headers={'Content-Length':'2'},rfile=io.BytesIO(b'{}'),_authorized=lambda:False,
            _json=lambda body,status=200:responses.append(status),
            _handle=lambda *args:self.fail('unauthenticated request changed control'))
        Handler.do_POST(request)
        self.assertEqual(responses,[401])

    def test_candidate_prediction_cannot_drive_adopted_ai(self):
        self.setup_predictions()
        with db.tx() as c:
            c.execute("INSERT INTO forecast_models(version,created,week,status,training_hash,artifact,metrics) VALUES(2,'2026-02-01','2026-02','candidate','synthetic-test2','{}','{}')")
        for horizon in ('3d','30m'):
            p=db.loads(db.q1('SELECT body FROM forecast_predictions WHERE horizon=?',(horizon,))['body'])
            p.update(id=p['id']+'candidate',model_version=2,probability=.1)
            ledger.record_prediction(p)
        q=self.quote();runtime._ai_decisions({'sh600000':q},self.current)
        row=db.q1('SELECT version,side FROM forecast_ai_orders')
        self.assertEqual((row['version'],row['side']),(1,'buy'))

    def test_opposite_new_signal_invalidates_pending_buy(self):
        self.setup_predictions();q=self.quote();runtime._ai_decisions({'sh600000':q},self.current)
        self.forecast('sh600000','3d',self.t+timedelta(seconds=1),probability=.2)
        self.current+=timedelta(seconds=3);q=self.quote()
        runtime._ai_decisions({'sh600000':q},self.current)
        self.assertEqual(db.q("SELECT * FROM trades WHERE account='ai'"),[],
                         'A valid but newly bearish forecast must invalidate an unfilled bullish entry')

if __name__=='__main__':unittest.main()
