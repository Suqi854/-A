import os, sys, tempfile, unittest, json, math
from unittest.mock import patch
from pathlib import Path
from datetime import datetime,timedelta,timezone
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
os.environ['AQ5_DATA']=tempfile.mkdtemp(prefix='forecast-model-tests-')
from aq5 import db
from aq5.forecast import models

class ModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init();models.init()

    def setUp(self):
        with db.tx() as c:
            c.execute('DELETE FROM forecast_models');c.execute('DELETE FROM forecast_model_attempts')

    def rows(self):
        start=datetime(2025,1,1,tzinfo=timezone(timedelta(hours=8)))
        return [{'id':f'{i}-{j}','code':f'sh60000{j}','asof':(start+timedelta(days=i)).isoformat(),
                 'due_at':(start+timedelta(days=i+1)).isoformat(),'horizon':'1d','origin':'historical','quality':{'eligible':True},
                 'features':{'chan_strength':(-1 if j%2 else 1)*(1+.01*i),'math_momentum':j%7-.5,
                             'math_duplicate':2*(j%7-.5)},
                 'outcome_return':(-.02 if j%2 else .02)} for i in range(65) for j in range(8)]

    def test_no_model_no_invented_probability(self):
        self.assertIsNone(models.predict({'chan_x':1},'1d')['probability'])

    def test_purge(self):
        folds=models._folds(self.rows())
        self.assertEqual(len(folds),3)
        for train,test in folds:
            self.assertLess(max(r['due_at'] for r in train),min(r['asof'] for r in test))

    def test_correlation_and_train_only_imputation(self):
        meta=models._preprocess(self.rows(),'math_only')
        self.assertEqual(len(meta),1)
        self.assertTrue(math.isfinite(models._vector({},meta)[0]))

    def test_real_training_and_json_roundtrip(self):
        try:import sklearn
        except ImportError:self.skipTest('optional sklearn dependency unavailable')
        rows=[{**r,'id':r['id']+h,'horizon':h} for h in models.HORIZONS for r in self.rows()]
        result=models.train_candidate(rows,asof='2026-01-01T16:00:00+08:00')
        self.assertTrue(result['trained'],result)
        p=models.predict({'chan_strength':2,'math_momentum':.5,'math_duplicate':1},'1d',model_version=result['version'])
        self.assertGreater(p['probability'],.5)
        self.assertTrue(p['validated'])
        self.assertEqual(p['status'],'candidate')
        self.assertEqual(sorted(p['quantiles'].values()),list(p['quantiles'].values()))
        row=db.q1('SELECT artifact FROM forecast_models')
        artifact=json.loads(row['artifact'])['models']['1d:fusion']
        direct=models._infer(artifact,{'chan_strength':2,'math_momentum':.5,'math_duplicate':1})
        self.assertAlmostEqual(p['probability'],direct['probability'])
        self.assertFalse(models.train_candidate(self.rows(),asof='2026-01-15T16:00:00+08:00')['trained'])
        self.assertIsNone(models.predict({},'1d')['probability'])

    def test_partial_bundle_does_not_freeze_candidate(self):
        result=models.train_candidate(self.rows(),asof='2026-01-01T16:00:00+08:00')
        self.assertFalse(result['trained'])
        self.assertIsNone(db.q1("SELECT * FROM forecast_models WHERE status='candidate'"))
        self.assertTrue(models.view()['attempts'])

    def test_quality_unknown_and_missingness_features_rejected(self):
        rows=self.rows()
        for r in rows:r.pop('quality')
        self.assertFalse(models.train_candidate(rows,asof='2026-01-01')['trained'])
        self.assertEqual(models._selected({'missing_chan_x':1,'chan_x':2,'math_x':3},'math_only'),{'math_x':3.})

    def test_read_api_no_write(self):
        with patch.object(db,'tx',side_effect=AssertionError('read must not write')):
            models.view();models.predict({},'1d')

    def test_immature_rows_not_trained(self):
        result=models.train_candidate(self.rows(),asof='2025-01-05T16:00:00+08:00')
        self.assertFalse(result['trained'])

if __name__=='__main__':unittest.main()
