"""Frozen, JSON-only forecast bundles. Fitting dependencies are optional at runtime."""
from __future__ import annotations
import hashlib, json, math
from datetime import datetime
from statistics import median
from .. import db
from ..config import now

VARIANTS = ('chan_only', 'math_only', 'fusion')
HORIZONS = ('1d', '3d', '5d', '20d', '30m', '60m')
PROTOCOL = 'forecast-model-v1'

def init():
    if db.q1("SELECT 1 FROM sqlite_master WHERE type='table' AND name='forecast_model_attempts'"):
        return
    if db.writes_frozen():
        return
    with db.tx() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS forecast_models(version INTEGER PRIMARY KEY AUTOINCREMENT,
          created TEXT NOT NULL, week TEXT NOT NULL UNIQUE, status TEXT NOT NULL, training_hash TEXT NOT NULL,
          artifact TEXT NOT NULL, metrics TEXT NOT NULL)''')
        c.execute('''CREATE TABLE IF NOT EXISTS forecast_model_attempts(id INTEGER PRIMARY KEY AUTOINCREMENT,
          created TEXT NOT NULL,training_hash TEXT NOT NULL,reason TEXT NOT NULL,metrics TEXT NOT NULL)''')

def finite(x):
    return isinstance(x, (float, int)) and not isinstance(x, bool) and math.isfinite(x)

def stamp(x):
    try:
        t = datetime.fromisoformat(str(x))
        return t.replace(tzinfo=now().tzinfo) if t.tzinfo is None else t
    except (ValueError, TypeError):
        return None

def _selected(features, variant):
    return {k: float(v) for k, v in features.items() if finite(v) and not k.startswith(('missing_','coverage_','data_')) and
            (variant == 'fusion' or (k.startswith('chan_') == (variant == 'chan_only')))}

def _preprocess(rows, variant):
    fs = [_selected(r['features'], variant) for r in rows]
    keys = sorted(set().union(*(f.keys() for f in fs)))
    meta, columns = [], []
    for key in keys:
        seen = [f[key] for f in fs if key in f]
        if len(seen) < max(10, len(fs) // 2):
            continue
        fill = median(seen)
        vals = [f.get(key, fill) for f in fs]
        mu = sum(vals) / len(vals)
        sd = math.sqrt(sum((v-mu)**2 for v in vals)/len(vals))
        if sd < 1e-12:
            continue
        col = [(v-mu)/sd for v in vals]
        if any(abs(sum(a*b for a,b in zip(col, other))/len(col)) >= .98 for other in columns):
            continue
        meta.append({'name':key,'median':fill,'mean':mu,'sd':sd})
        columns.append(col)
    return meta

def _vector(features, meta):
    return [(float(features[m['name']]) if finite(features.get(m['name'])) else m['median'])
            / m['sd'] - m['mean']/m['sd'] for m in meta]

def _tree(tree):
    t=tree.tree_
    return {'left':t.children_left.tolist(),'right':t.children_right.tolist(),
            'feature':t.feature.tolist(),'threshold':t.threshold.tolist(),
            'value':t.value[:,0,0].tolist()}

def _value(tree, x):
    n=0
    while tree['left'][n] != -1:
        n = tree['left'][n] if x[tree['feature'][n]] <= tree['threshold'][n] else tree['right'][n]
    return tree['value'][n]

def _sig(x):
    return 1/(1+math.exp(-max(-700,min(700,x))))

def _fit(rows, variant):
    from sklearn.linear_model import LogisticRegression
    from sklearn.ensemble import GradientBoostingClassifier, GradientBoostingRegressor
    meta=_preprocess(rows,variant)
    if not meta:
        raise ValueError('no_nonredundant_features')
    X=[_vector(r['features'],meta) for r in rows]
    returns=[r['outcome_return'] for r in rows]
    y=[int(r>0) for r in returns]
    if len(set(y))<2:
        raise ValueError('single_class')
    lr=LogisticRegression(C=.25,max_iter=500,random_state=17).fit(X,y)
    gb=GradientBoostingClassifier(n_estimators=40,max_depth=2,min_samples_leaf=10,
                                 learning_rate=.05,random_state=17).fit(X,y)
    artifact={'features':meta,'logistic':{'coef':lr.coef_[0].tolist(),'intercept':float(lr.intercept_[0])},
              'classifier':{'initial':float(gb._raw_predict_init([X[0]])[0,0]),'rate':gb.learning_rate,
                            'trees':[_tree(t[0]) for t in gb.estimators_]},'quantiles':{},
              'prior':sum(y)/len(y)}
    for label,alpha in [('p10',.1),('p50',.5),('p90',.9)]:
        q=GradientBoostingRegressor(loss='quantile',alpha=alpha,n_estimators=40,max_depth=2,
                                   min_samples_leaf=10,learning_rate=.05,random_state=17).fit(X,returns)
        artifact['quantiles'][label]={'initial':float(q.init_.constant_[0,0]),'rate':q.learning_rate,
                                     'trees':[_tree(t[0]) for t in q.estimators_]}
    return artifact

def _infer(artifact, features):
    x=_vector(features,artifact['features'])
    lr=artifact['logistic']; gb=artifact['classifier']
    p=_sig(gb['initial']+gb['rate']*sum(_value(t,x) for t in gb['trees']))
    lp=_sig(lr['intercept']+sum(a*b for a,b in zip(lr['coef'],x)))
    values=sorted(q['initial']+q['rate']*sum(_value(t,x) for t in q['trees']) for q in artifact['quantiles'].values())
    return {'probability':p,'logistic_probability':lp,'prior_probability':artifact['prior'],
            'quantiles':dict(zip(('p10','p50','p90'),values))}

def _folds(rows):
    dates=sorted({str(r['asof'])[:10] for r in rows})
    if len(dates)<30:
        return []
    width=max(5,len(dates)//6)
    folds=[]
    for start in range(len(dates)-3*width,len(dates),width):
        test_dates=set(dates[start:start+width]); boundary=stamp(dates[start]+'T00:00:00')
        train=[r for r in rows if stamp(r['due_at'])<boundary]
        test=[r for r in rows if str(r['asof'])[:10] in test_dates]
        if len(train)>=60 and len(test)>=20:
            folds.append((train,test))
    return folds

def train_candidate(rows, asof=None):
    from .features import VERSION as feature_version
    # Optional local installation stays beside the engine on its data drive.
    # Cloud runners install the same pinned requirements into their environment.
    import sys
    from ..config import ROOT
    local_dependencies=ROOT/'vendor'/'forecast_deps'
    if local_dependencies.is_dir() and str(local_dependencies) not in sys.path:
        sys.path.insert(0,str(local_dependencies))
    init(); t=stamp(asof) if asof else now()
    if not t:
        raise ValueError('invalid asof')
    week=f'{t.isocalendar().year}-{t.isocalendar().week:02}'
    if db.q1("SELECT 1 FROM forecast_models WHERE week=? OR status='candidate'",(week,)):
        return {'trained':False,'reason':'weekly_limit_or_frozen_candidate'}
    try:
        import sklearn
    except ImportError:
        return {'trained':False,'reason':'sklearn_unavailable'}
    valid=[]; seen=set()
    for r in rows:
        if r.get('feature_version') not in (None,feature_version):
            continue
        a,d=stamp(r.get('asof')),stamp(r.get('due_at'))
        if (not a or not d or not a<d<=t or not finite(r.get('outcome_return')) or
            not isinstance(r.get('features'),dict) or r.get('horizon') not in HORIZONS or
            r.get('origin') not in ('forward','historical') or (r.get('quality') or {}).get('eligible') is not True):
            continue
        identity=(r.get('code'),r.get('horizon'),r.get('asof'),r.get('due_at'))
        observed=stamp(r.get('outcome_observed_at'))
        if r.get('origin')=='forward' and (not observed or not d<=observed<=t):
            continue
        if not r.get('id') or not r.get('code') or identity in seen:
            continue
        seen.add(identity); valid.append({**r,'features':{k:float(v) for k,v in r['features'].items() if finite(v)}})
    valid.sort(key=lambda r:(r['asof'],str(r['id'])))
    digest=hashlib.sha256(json.dumps(valid,sort_keys=True,ensure_ascii=False,allow_nan=False).encode()).hexdigest()
    if db.q1('SELECT 1 FROM forecast_model_attempts WHERE training_hash=?',(digest,)):
        return {'trained':False,'reason':'unchanged_training_evidence'}
    artifacts, metrics={},{}
    for horizon in HORIZONS:
        subset=[r for r in valid if r['horizon']==horizon]; folds=_folds(subset)
        for variant in VARIANTS:
            key=horizon+':'+variant
            try:
                if len(folds)<3:
                    raise ValueError('three_purged_folds_required')
                scores=[]
                for train,test in folds:
                    fit=_fit(train,variant); pred=[_infer(fit,r['features']) for r in test]
                    brier=sum((p['probability']-int(r['outcome_return']>0))**2 for p,r in zip(pred,test))/len(test)
                    prior=sum((fit['prior']-int(r['outcome_return']>0))**2 for r in test)/len(test)
                    scores.append({'brier':brier,'prior_brier':prior,'n':len(test),
                                   'train_label_end':max(r['due_at'] for r in train),'test_start':min(r['asof'] for r in test)})
                artifacts[key]=_fit(subset,variant)
                metrics[key]={'folds':scores,'oos_passed':sum(s['brier']*s['n'] for s in scores)<sum(s['prior_brier']*s['n'] for s in scores)}
            except ValueError as exc:
                metrics[key]={'oos_passed':False,'reason':str(exc)}
    if len(artifacts)!=len(HORIZONS)*len(VARIANTS) or not all(metrics.get(h+':fusion',{}).get('oos_passed') for h in HORIZONS):
        reason='incomplete_bundle_or_failed_oos'
        with db.tx() as c:
            c.execute('INSERT INTO forecast_model_attempts(created,training_hash,reason,metrics) VALUES(?,?,?,?)',
                      (t.isoformat(),digest,reason,db.dumps(metrics)))
        return {'trained':False,'reason':reason,'metrics':metrics}
    artifact={'protocol':PROTOCOL,'feature_version':feature_version,'sklearn_version':sklearn.__version__,'models':artifacts}
    with db.tx() as c:
        if c.execute("SELECT 1 FROM forecast_models WHERE week=? OR status='candidate'",(week,)).fetchone():
            return {'trained':False,'reason':'weekly_limit_or_frozen_candidate'}
        cur=c.execute('INSERT INTO forecast_models(created,week,status,training_hash,artifact,metrics) VALUES(?,?,?,?,?,?)',
                      (t.isoformat(),week,'candidate',digest,db.dumps(artifact),db.dumps(metrics)))
    return {'trained':True,'version':cur.lastrowid,'status':'candidate','metrics':metrics}

def predict(features,horizon,variant='fusion',model_version=None):
    from .features import VERSION as feature_version
    init()
    empty={'probability':None,'quantiles':{'p10':None,'p50':None,'p90':None},'model_version':model_version,
           'validated':False,'reason':'概率未验证'}
    if horizon not in HORIZONS or variant not in VARIANTS:
        return {**empty,'reason':'unsupported_horizon_or_variant'}
    if not db.q1("SELECT 1 FROM sqlite_master WHERE name='forecast_models'"):
        return empty
    row=db.q1('SELECT * FROM forecast_models WHERE version=?',(model_version,)) if model_version is not None else db.q1("SELECT * FROM forecast_models WHERE status IN ('candidate','active') ORDER BY version DESC LIMIT 1")
    if not row:
        return empty
    bundle=db.loads(row['artifact'])
    if bundle.get('feature_version') not in (None,feature_version):
        return {**empty,'model_version':row['version'],'reason':'feature_version_changed'}
    key=horizon+':'+variant; artifact=bundle['models'].get(key)
    if not artifact:
        return {**empty,'model_version':row['version'],'reason':'horizon_not_trained'}
    if sum(finite(features.get(f['name'])) for f in artifact['features']) < max(1,math.ceil(.8*len(artifact['features']))):
        return {**empty,'model_version':row['version'],'reason':'feature_coverage_insufficient'}
    result=_infer(artifact,features); metric=db.loads(row['metrics']).get(key,{})
    return {**result,'model_version':row['version'],'validated':bool(metric.get('oos_passed')),
            'status':row['status'],'reason':'前瞻验证中' if row['status']!='active' else 'active'}

def view():
    init()
    if not db.q1("SELECT 1 FROM sqlite_master WHERE name='forecast_models'"):
        return {'protocol':PROTOCOL,'versions':[],'attempts':[]}
    return {'protocol':PROTOCOL,'versions':[{**r,'metrics':db.loads(r['metrics'])} for r in db.q(
        'SELECT version,created,week,status,training_hash,metrics FROM forecast_models ORDER BY version DESC LIMIT 30')],
        'attempts':[{**r,'metrics':db.loads(r['metrics'])} for r in db.q('SELECT * FROM forecast_model_attempts ORDER BY id DESC LIMIT 10')]}
