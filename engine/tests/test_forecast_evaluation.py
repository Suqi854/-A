import os,sys,tempfile,unittest,copy
from pathlib import Path
from datetime import date,timedelta
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
os.environ['AQ5_DATA']=tempfile.mkdtemp(prefix='forecast-eval-tests-')
from aq5 import db
from aq5.forecast import models,evaluation as ev

def fixture():
    days=[];t=date(2026,1,5)
    while len(days)<60:
        if t.weekday()<5:days.append(t.isoformat())
        t+=timedelta(days=1)
    return {'origin':'forward','complete':True,'closed_trades':60,'missing':0,'failed':0,
            'daily':[{'date':d,'origin':'forward','candidate_return':.002,'baseline_return':.001,
                      'candidate_cost':.0001,'baseline_cost':.0001,'candidate_stress_return':.00195,'baseline_stress_return':.00095} for d in days],
            'predictions':[{'id':f'{h}-{i}','horizon':h,'origin':'forward','status':'settled',
                            'outcome_return':.01,'probability':.8,'prior_probability':.5,
                            'quantiles':{'p10':0,'p90':.02 if i%5 else .005}} for h in models.HORIZONS for i in range(300)]}

def identified(e,version):
    e['model_version']=version
    for p in e['predictions']:
        p.update(model_version=version,asof='2026-01-05T10:00:00+08:00',due_at='2026-02-05T15:00:00+08:00')
    return e

class EvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):db.init();models.init();ev.init()

    def setUp(self):
        with db.tx() as c:
            for t in ('forecast_models','forecast_controls','forecast_evaluations'):c.execute('DELETE FROM '+t)

    def candidate(self, metrics=True):
        with db.tx() as c:
            return c.execute('INSERT INTO forecast_models(created,week,status,training_hash,artifact,metrics) VALUES(?,?,?,?,?,?)',
                ('2026-01-01T16:00:00+08:00','2026-01','candidate','hash','{}',
                 db.dumps({h+':fusion':{'oos_passed':metrics} for h in models.HORIZONS}))).lastrowid

    def test_gate_pass_synthetic_not_market_proof(self):
        report=ev.evaluate(fixture());self.assertTrue(report['passed'],report)

    def test_fail_closed_missing_nan_history_duplicates(self):
        for mutation in ('missing','nan','history','duplicate','cost'):
            e=fixture()
            if mutation=='missing':e['complete']=False
            if mutation=='nan':e['predictions'][0]['probability']=float('nan')
            if mutation=='history':e['predictions'][0]['origin']='historical'
            if mutation=='duplicate':e['daily'].append(e['daily'][0])
            if mutation=='cost':e['daily'][0]['candidate_cost']=None
            self.assertFalse(ev.evaluate(e)['passed'],mutation)

    def test_adopt_and_atomic_single_exam(self):
        version=self.candidate();e=identified(fixture(),version)
        r=ev.decide(version,e,'2026-05-01T16:00:00+08:00')
        self.assertEqual(r['decision'],'adopted',r)
        self.assertEqual(ev.controls()['active_version'],version)
        self.assertEqual(ev.decide(version,e,'2026-05-02T16:00:00+08:00')['decision'],'not_candidate')
        self.assertEqual(len(db.q('SELECT * FROM forecast_evaluations')),1)

    def test_oos_required_and_consumed_failure(self):
        version=self.candidate(False)
        self.assertEqual(ev.decide(version,identified(fixture(),version),'2026-05-01T16:00:00+08:00')['decision'],'rejected')
        self.assertEqual(ev.controls()['active_version'],0)

    def test_pre_candidate_window_cannot_promote(self):
        version=self.candidate()
        with db.tx() as c:c.execute("UPDATE forecast_models SET created='2026-02-01' WHERE version=?",(version,))
        self.assertEqual(ev.decide(version,identified(fixture(),version),'2026-05-01T16:00:00+08:00')['decision'],'invalid_prediction_identity_or_time')

    def test_monitor_new_window_and_rollback(self):
        version=self.candidate();ev.decide(version,identified(fixture(),version),'2026-05-01T16:00:00+08:00')
        e=identified(fixture(),version);self.assertEqual(ev.monitor(e,'2026-05-02T16:00:00+08:00')['decision'],'waiting')
        days=[];t=date(2026,5,4)
        while len(days)<20:
            if t.weekday()<5:days.append(t.isoformat())
            t+=timedelta(days=1)
        e['daily']=[{**e['daily'][0],'date':d,'candidate_return':-.01} for d in days]
        self.assertEqual(ev.monitor(e,'2026-07-01T16:00:00+08:00')['decision'],'rollback')
        self.assertEqual(ev.controls()['active_version'],0)
        e['complete']=False
        self.assertEqual(ev.monitor(e)['decision'],'paused_data')
        self.assertTrue(ev.controls()['paused'])

if __name__=='__main__':unittest.main()
