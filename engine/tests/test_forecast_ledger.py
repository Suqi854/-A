import copy
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ['AQ5_DATA'] = tempfile.mkdtemp(prefix='ledger-', dir=os.environ['AQ5_DATA'])
from aq5 import db, market
from aq5.config import CN_TZ
from aq5.forecast import ledger


class LedgerTests(unittest.TestCase):
    code = 'sh600000'
    other = 'sz000001'
    t = datetime(2026, 11, 9, 10, tzinfo=CN_TZ)

    def setUp(self):
        db.init(); ledger.init()
        with db.tx() as c:
            for table in ('forecast_closed_trades','forecast_cycles','forecast_fills','forecast_orders',
                          'forecast_marks','forecast_books','forecast_results','forecast_predictions','forecast_ledger_events'):
                c.execute('DELETE FROM '+table)

    def quote(self, t=None, price=10., **kwargs):
        t = t or self.t
        return {'price':price,'time':t.isoformat(),'received_at':t.isoformat(),'source':'fixture',
                'limit_up':11.,'limit_down':9.,**kwargs}

    def opening(self, cash=10000., qty=300, **kwargs):
        holds = [{'code':self.code,'qty':qty,'cost':8.,'acquired_at':'2026-11-06T10:00:00+08:00'}] if qty else []
        return ledger.initialize(cash, holds, {self.code:self.quote()}, self.t, **kwargs)

    def prediction(self, id='p1', code=None, book='fusion', t=None, due=None, **extra):
        t = t or self.t
        item = {'id':id,'code':code or self.code,'horizon':'30m','asof':t.isoformat(),
                'due_at':(due or t+timedelta(minutes=30)).isoformat(),'reference_price':10.,
                'source_time':t.isoformat(),'features':{'trend':.2},'structure_id':'s1','model_version':'v1',
                'variant':book,'probability':.6,'prior_probability':.5,
                'quantiles':{'p10':-.02,'p50':.01,'p90':.03},'quality':{'eligible':True},'origin':'forward',**extra}
        ledger.record_prediction(item)
        return item

    def test_prediction_immutable_idempotent(self):
        p = self.prediction()
        self.assertTrue(ledger.record_prediction(p)['existing'])
        p['probability'] = .7
        with self.assertRaisesRegex(ValueError, 'immutable'):
            ledger.record_prediction(p)
        self.assertEqual(len(ledger.predictions()),1)

    def test_invalid_probability_future_quote_and_provenance(self):
        for extra in ({'probability':float('nan')},{'source_time':'2026-11-10T10:00:00+08:00'},
                      {'origin':'real'},{'quantiles':{'p10':.2,'p50':.1,'p90':.3}}):
            with self.assertRaises(ValueError): self.prediction(**extra)

    def test_settle_after_deadline_only(self):
        self.prediction()
        self.assertEqual(ledger.settle({self.code:self.quote()}, self.t)['settled'],0)
        due = self.t+timedelta(minutes=30)
        self.assertEqual(ledger.settle({self.code:self.quote(due,10.2)},due)['settled'],1)
        self.assertAlmostEqual(ledger.training_rows()[0]['outcome_return'],.02)
        self.assertEqual(ledger.settle({self.code:self.quote(due,10.8)},due)['settled'],0)

    def test_gap_is_unknown_not_later_success(self):
        self.prediction()
        late = self.t+timedelta(minutes=31)
        self.assertEqual(ledger.settle({self.code:self.quote(late,10.9)},late)['unknown'],1)
        self.assertEqual(ledger.predictions()[0]['result']['status'],'unknown')
        self.assertEqual(ledger.training_rows(),[])

    def test_invalid_prediction_cannot_trade(self):
        self.opening()
        self.prediction(quality={'eligible':False})
        r = ledger.submit('fusion',self.code,'buy',100,'p1',self.quote(),self.t)
        self.assertEqual(r['reason'],'prediction_ineligible')

    def test_equal_openings_and_no_rebase(self):
        first = self.opening()
        self.assertTrue(self.opening()['existing'])
        books = ledger.view()['books']
        self.assertEqual(len(books),4)
        self.assertEqual(len({b['cash'] for b in books.values()}),1)
        self.assertEqual(len({str(b['baseline']) for b in books.values()}),1)
        with self.assertRaisesRegex(ValueError,'cannot rebase'): self.opening(cash=20000)
        self.assertTrue(first['frozen_key'])

    def test_short_and_stale_openings_rejected(self):
        with self.assertRaises(ValueError): self.opening(qty=-100)
        with self.assertRaises(ValueError):
            ledger.initialize(10,[{'code':self.code,'qty':100}],{self.code:self.quote(self.t-timedelta(seconds=20))},self.t)

    def test_next_quote_required_and_retry_idempotent(self):
        self.opening(); self.prediction()
        r = ledger.submit('fusion',self.code,'buy',100,'p1',self.quote(),self.t)
        self.assertEqual(r['status'],'pending')
        self.assertEqual(ledger.match({self.code:self.quote()},self.t)['filled'],0)
        later = self.t+timedelta(seconds=3)
        self.assertEqual(ledger.match({self.code:self.quote(later)},later)['filled'],1)
        self.assertEqual(ledger.match({self.code:self.quote(later)},later)['filled'],0)
        self.assertEqual(ledger.submit('fusion',self.code,'buy',100,'p1',self.quote(),self.t)['status'],'filled')
        self.assertEqual(ledger.view()['books']['fusion']['holdings'][self.code]['qty'],400)

    def test_buy_reservations_prevent_overspend(self):
        self.opening(cash=1500); self.prediction(); self.prediction('p2',self.other)
        a = ledger.submit('fusion',self.code,'buy',100,'p1',self.quote(),self.t)
        b = ledger.submit('fusion',self.other,'buy',100,'p2',self.quote(),self.t)
        self.assertEqual(a['status'],'pending')
        self.assertEqual(b['reason'],'insufficient_cash')

    def test_sell_reservations_and_whole_lots(self):
        self.opening(qty=100); self.prediction(); self.prediction('p2')
        self.assertEqual(ledger.submit('fusion',self.code,'sell',100,'p1',self.quote(),self.t)['status'],'pending')
        self.assertEqual(ledger.submit('fusion',self.code,'sell',100,'p2',self.quote(),self.t)['reason'],'insufficient_sellable_t1')
        with self.assertRaises(ValueError): ledger.submit('fusion',self.code,'buy',50,'p3',self.quote(),self.t)

    def test_t1_new_buy_locked(self):
        self.opening(qty=0); self.prediction()
        ledger.submit('fusion',self.code,'buy',100,'p1',self.quote(),self.t)
        later = self.t+timedelta(seconds=3)
        ledger.match({self.code:self.quote(later)},later)
        self.prediction('p2',t=later)
        self.assertEqual(ledger.submit('fusion',self.code,'sell',100,'p2',self.quote(later),later)['reason'],'insufficient_sellable_t1')
        tomorrow = self.t+timedelta(days=1)
        self.prediction('p3',t=tomorrow)
        self.assertEqual(ledger.submit('fusion',self.code,'sell',100,'p3',self.quote(tomorrow),tomorrow)['status'],'pending')

    def test_stale_limits_suspension_and_missing_limits(self):
        self.opening()
        cases = [(self.quote(self.t-timedelta(seconds=13)),'stale_quote'),
                 (self.quote(price=11.),'price_limit_locked'),
                 (self.quote(suspended=True),'suspended'),
                 (self.quote(limit_up=None),'missing_price_limits')]
        for i,(q,expected) in enumerate(cases):
            pid = 'p'+str(i); self.prediction(pid)
            self.assertEqual(ledger.submit('fusion',self.code,'buy',100,pid,q,self.t)['reason'],expected)

    def test_source_change_and_expiry(self):
        self.opening(); self.prediction()
        ledger.submit('fusion',self.code,'buy',100,'p1',self.quote(),self.t)
        later = self.t+timedelta(seconds=3)
        self.assertEqual(ledger.match({self.code:self.quote(later,source='other')},later)['filled'],0)
        self.assertEqual(ledger.match({},self.t+timedelta(minutes=6))['expired'],1)

    def test_round_trip_and_fees(self):
        self.opening(); self.prediction()
        ledger.submit('fusion',self.code,'sell',100,'p1',self.quote(price=10.5),self.t,intent='t')
        later = self.t+timedelta(seconds=3)
        ledger.match({self.code:self.quote(later,10.5)},later)
        self.assertEqual(len(ledger.view()['books']['fusion']['open_cycles']),1)
        again = later+timedelta(minutes=1)
        self.prediction('p2',t=again)
        ledger.submit('fusion',self.code,'buy',100,'p2',self.quote(again,10.),again,intent='t')
        ledger.match({self.code:self.quote(again+timedelta(seconds=3),10.)},again+timedelta(seconds=3))
        closed = db.q('SELECT * FROM forecast_closed_trades')
        self.assertEqual(len(closed),1)
        self.assertGreater(closed[0]['net_fen'],0)
        self.assertGreater(closed[0]['costs_fen'],1000)

    def test_restart_no_duplicate_fill(self):
        self.opening(); self.prediction()
        ledger.submit('fusion',self.code,'buy',100,'p1',self.quote(),self.t)
        db.conn().close(); del db._local.c
        later = self.t+timedelta(seconds=3)
        self.assertEqual(ledger.match({self.code:self.quote(later)},later)['filled'],1)
        ledger.init()
        self.assertEqual(ledger.match({self.code:self.quote(later)},later)['filled'],0)
        self.assertEqual(db.q1('SELECT COUNT(*) AS n FROM forecast_fills')['n'],1)

    def test_close_only_and_same_quote_marks(self):
        self.opening()
        ledger.initialize_version(1,{self.code:self.quote()},self.t)
        self.assertFalse(ledger.mark({self.code:self.quote()},self.t)['complete'])
        close = self.t.replace(hour=15)
        q = self.quote(close,10.2)
        self.assertTrue(ledger.mark({self.code:q},close,model_version=1)['complete'])
        evidence = ledger.evaluation_data(model_version=1)
        self.assertEqual(len(evidence['daily']),1)
        self.assertEqual(evidence['daily'][0]['candidate_return'],evidence['daily'][0]['baseline_return'])
        self.assertEqual(len(ledger.evaluation_data(model_version=2)['daily']),0)
        ledger.mark({self.code:self.quote(close,10.8)},close,model_version=1)
        self.assertEqual(ledger.evaluation_data(model_version=1)['daily'][0]['candidate_return'],evidence['daily'][0]['candidate_return'])

    def test_missing_close_kept_unknown(self):
        self.opening()
        close = self.t.replace(hour=15)
        self.assertFalse(ledger.mark({self.code:self.quote(close-timedelta(minutes=1))},close)['complete'])
        self.assertFalse(ledger.evaluation_data()['complete'])
        self.assertEqual(ledger.evaluation_data()['missing'],1)

    def test_legacy_control_has_same_execution_guards(self):
        self.opening(qty=0)
        r = ledger.submit('legacy_control',self.code,'sell',100,'legacy-sell',self.quote(),self.t)
        self.assertEqual(r['reason'],'insufficient_sellable_t1')
        self.assertEqual(ledger.submit('legacy_control',self.code,'buy',100,'legacy-buy',self.quote(),self.t)['status'],'pending')

    def test_exact_stress_fee_tick_and_return(self):
        self.opening(); self.prediction(model_version=1)
        ledger.initialize_version(1,{self.code:self.quote()},self.t)
        ledger.submit('fusion@1',self.code,'buy',100,'p1',self.quote(),self.t)
        later = self.t+timedelta(seconds=3)
        ledger.match({self.code:self.quote(later)},later)
        fill = db.q1('SELECT * FROM forecast_fills')
        # 10.00 -> normal 10.01; stress 10.02 under integer-fen rounding.
        expected_price = ledger._price('buy',10.,.0015)
        expected_fee = ledger._fees('buy',expected_price,100,{k:v*1.5 for k,v in ledger.FEES.items()})
        exact = (expected_price-fill['price_fen'])*100+expected_fee-fill['fee_fen']
        self.assertEqual(fill['stress_extra_fen'],exact)
        close = self.t.replace(hour=15)
        ledger.mark({self.code:self.quote(close)},close,model_version=1)
        evidence = ledger.evaluation_data(model_version=1)
        self.assertEqual(evidence['model_version'],1)
        self.assertLess(evidence['daily'][0]['candidate_stress_return'],evidence['daily'][0]['candidate_return'])

    def test_unknown_quote_does_not_free_cash_reservations(self):
        self.opening(cash=1500); self.prediction(); self.prediction('p2',self.other)
        ledger.submit('fusion',self.code,'buy',100,'p1',self.quote(),self.t)
        later = self.t+timedelta(seconds=3)
        self.assertEqual(ledger.match({},later)['blocked'],1)
        r = ledger.submit('fusion',self.other,'buy',100,'p2',self.quote(later),later)
        self.assertEqual(r['reason'],'insufficient_cash')

    def test_pause_cancels_pending_and_release_cash(self):
        self.opening(cash=1500); self.prediction(); self.prediction('p2',self.other)
        ledger.submit('fusion',self.code,'buy',100,'p1',self.quote(),self.t)
        self.assertEqual(ledger.cancel_pending('data_gap',self.t),1)
        self.assertEqual(ledger.submit('fusion',self.other,'buy',100,'p2',self.quote(),self.t)['status'],'pending')

    def test_fractional_fee_allocation_preserves_fen(self):
        self.opening(qty=300); self.prediction()
        ledger.submit('fusion',self.code,'sell',300,'p1',self.quote(price=10.5),self.t)
        t = self.t+timedelta(seconds=3)
        ledger.match({self.code:self.quote(t,10.5)},t)
        open_fee = db.q1('SELECT fee_fen FROM forecast_fills')['fee_fen']
        for i in range(3):
            t += timedelta(minutes=1)
            pid = 'back'+str(i); self.prediction(pid,t=t)
            ledger.submit('fusion',self.code,'buy',100,pid,self.quote(t),t)
            t += timedelta(seconds=3)
            ledger.match({self.code:self.quote(t)},t)
        rows = db.q('SELECT * FROM forecast_closed_trades')
        fills = db.q("SELECT * FROM forecast_fills WHERE side='buy'")
        gross = sum((1049-f['price_fen'])*f['qty'] for f in fills)
        # The sum of net round-trip PnL contains each fen of opening fees once.
        actual_sale = db.q1("SELECT price_fen FROM forecast_fills WHERE side='sell'")['price_fen']
        gross = sum((actual_sale-f['price_fen'])*f['qty'] for f in fills)
        self.assertEqual(sum(r['net_fen'] for r in rows),gross-open_fee-sum(f['fee_fen'] for f in fills))
        self.assertEqual(ledger.evaluation_data()['closed_trades'],1)  # three partial fills are one closed cycle

    def test_partial_cycle_not_counted_closed(self):
        self.opening(qty=300); self.prediction()
        ledger.submit('fusion',self.code,'sell',300,'p1',self.quote(price=10.5),self.t)
        t = self.t+timedelta(seconds=3)
        ledger.match({self.code:self.quote(t,10.5)},t)
        t += timedelta(minutes=1)
        self.prediction('p2',t=t)
        ledger.submit('fusion',self.code,'buy',100,'p2',self.quote(t),t)
        t += timedelta(seconds=3)
        ledger.match({self.code:self.quote(t)},t)
        self.assertEqual(ledger.evaluation_data()['closed_trades'],0)
        self.assertEqual(ledger.evaluation_data()['open_cycles'],1)

    def test_missing_intermediate_close_day_blocks_complete(self):
        self.opening()
        ledger.initialize_version(1,{self.code:self.quote()},self.t)
        close = self.t.replace(hour=15)
        ledger.mark({self.code:self.quote(close)},close,model_version=1)
        later = close+timedelta(days=2)
        ledger.mark({self.code:self.quote(later)},later,model_version=1)
        evidence = ledger.evaluation_data(model_version=1)
        self.assertFalse(evidence['complete'])
        self.assertEqual(evidence['missing_mark_dates'],['2026-11-10'])

    def test_model_epochs_are_parallel_and_never_reset_parent(self):
        self.opening()
        root_before=ledger.view()['books']['legacy_control']
        ledger.initialize_version(1,{self.code:self.quote()},self.t)
        self.prediction(model_version=1)
        ledger.submit('fusion@1',self.code,'buy',100,'p1',self.quote(),self.t)
        later=self.t+timedelta(seconds=3)
        ledger.match({self.code:self.quote(later)},later)
        epoch1_before=ledger.view()['books']['fusion@1']
        ledger.initialize_version(2,{self.code:self.quote(later)},later)
        snapshot=ledger.view()['books']
        self.assertEqual(len(snapshot),12)
        self.assertEqual(snapshot['fusion@1']['cash'],epoch1_before['cash'])
        self.assertEqual(snapshot['legacy_control']['baseline'],root_before['baseline'])
        self.assertEqual(snapshot['fusion@2']['cash'],root_before['cash'])
        self.assertEqual(snapshot['fusion@2']['baseline']['root_baseline'],root_before['baseline'])
        self.assertEqual(snapshot['fusion@2']['model_version'],2)
        # A restart does not revalue or refreeze an already-created candidate.
        self.assertTrue(ledger.initialize_version(1,{},later+timedelta(minutes=1))['existing'])
        self.assertEqual(ledger.view()['books']['fusion@1']['cash'],epoch1_before['cash'])

    def test_epoch_prediction_identity_and_control_guards(self):
        self.opening()
        ledger.initialize_version(1,{self.code:self.quote()},self.t)
        self.prediction(model_version=2)
        r=ledger.submit('fusion@1',self.code,'buy',100,'p1',self.quote(),self.t)
        self.assertEqual(r['reason'],'prediction_wrong_epoch')
        baseline=ledger.submit('legacy_control@1',self.code,'buy',100,'control1',self.quote(),self.t)
        self.assertEqual(baseline['status'],'pending')
        self.assertEqual(ledger.evaluation_data(model_version=2)['reason'],'unconfigured')

    def test_close_marks_each_epoch_even_when_root_passes_newest(self):
        self.opening()
        ledger.initialize_version(1,{self.code:self.quote()},self.t)
        ledger.initialize_version(2,{self.code:self.quote()},self.t)
        close=self.t.replace(hour=15)
        ledger.mark({self.code:self.quote(close)},close,model_version=2)
        self.assertEqual(ledger.evaluation_data(model_version=1)['daily'][0]['model_version'],1)
        self.assertEqual(ledger.evaluation_data(model_version=2)['daily'][0]['model_version'],2)

    def test_baseline_data_gap_blocks_only_its_epoch(self):
        self.opening()
        ledger.initialize_version(1,{self.code:self.quote()},self.t)
        ledger.initialize_version(2,{self.code:self.quote()},self.t)
        close=self.t.replace(hour=15)
        ledger.mark({self.code:self.quote(close)},close)
        with db.tx() as c:
            ledger._event(c,'legacy_data_gap','gap1',self.t,{'book':'legacy_control@1','reason':'missing_peer'})
        self.assertFalse(ledger.evaluation_data(model_version=1)['complete'])
        self.assertEqual(ledger.evaluation_data(model_version=1)['legacy_gap_events'],1)
        self.assertTrue(ledger.evaluation_data(model_version=2)['complete'])

    def test_real_tencent_received_milliseconds_survive_open_submit_match(self):
        received=self.t.replace(microsecond=800000)
        evaluated=self.t.replace(microsecond=900000)
        source={'price':10.,'time':self.t.strftime('%Y%m%d%H%M%S'),'up_limit':11.,'dn_limit':9.}
        with patch.object(market,'now',return_value=received):
            quote=market._remember_tencent({self.code:source})[self.code]
        self.assertEqual(quote['received_at'],'2026-11-09T10:00:00.800+08:00')
        ledger.initialize(10000,[{'code':self.code,'qty':100,'acquired_at':'2026-11-06T10:00:00+08:00'}],
                          {self.code:quote},evaluated)
        self.prediction(t=evaluated,source_time=self.t.isoformat())
        pending=ledger.submit('fusion',self.code,'buy',100,'p1',quote,evaluated)
        self.assertEqual(pending['status'],'pending')
        self.assertEqual(db.q1('SELECT submitted_at FROM forecast_orders')['submitted_at'],evaluated.isoformat())
        # A changed price received later in the same source second is not a next quote.
        with patch.object(market,'now',return_value=self.t.replace(microsecond=950000)):
            same=market._remember_tencent({self.code:{**source,'price':10.01}})[self.code]
        self.assertEqual(ledger.match({self.code:same},self.t.replace(microsecond=960000))['filled'],0)
        next_source=self.t+timedelta(seconds=1)
        with patch.object(market,'now',return_value=next_source.replace(microsecond=50000)):
            next_quote=market._remember_tencent({self.code:{**source,'time':next_source.strftime('%Y%m%d%H%M%S')}})[self.code]
        self.assertEqual(ledger.match({self.code:next_quote},next_source.replace(microsecond=100000))['filled'],1)

    def test_fractional_future_and_stale_checks_remain_strict(self):
        evaluated=self.t.replace(microsecond=900000)
        quote=self.quote(self.t,received_at=(evaluated+timedelta(microseconds=1)).isoformat())
        self.assertEqual(ledger._quote(quote,evaluated)[1],'future_quote')
        source=evaluated-timedelta(seconds=12,microseconds=1)
        self.assertEqual(ledger._quote(self.quote(source),evaluated)[1],'stale_quote')
        source=evaluated-timedelta(seconds=12)
        self.assertIsNone(ledger._quote(self.quote(source),evaluated)[1])

    def test_fractional_source_order_and_deadline_are_not_truncated(self):
        start=self.t.replace(microsecond=500000)
        due=start+timedelta(minutes=30)
        self.prediction(t=start,due=due)
        before=due-timedelta(microseconds=1)
        self.assertEqual(ledger.settle({self.code:self.quote(before,10.2)},before)['settled'],0)
        self.assertEqual(ledger.settle({self.code:self.quote(before,10.2)},due)['settled'],0)
        after=due+timedelta(microseconds=100000)
        q=self.quote(after,10.2,received_at=(after+timedelta(microseconds=1000)).isoformat())
        self.assertEqual(ledger.settle({self.code:q},after+timedelta(microseconds=2000))['settled'],1)
        stored=ledger.predictions()[0]
        self.assertEqual(stored['asof'],start.isoformat())
        self.assertEqual(stored['due_at'],due.isoformat())
        self.assertEqual(stored['result']['quote']['time'],after.isoformat())


if __name__ == '__main__':
    unittest.main()
