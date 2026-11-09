"""Fail-closed, forward-only promotion and rollback with consumed evaluation windows."""
from __future__ import annotations
import hashlib, json, math, random
from .. import db
from ..config import now
from .models import HORIZONS, finite, stamp

PROTOCOL='forecast-gates-v1'
THRESHOLDS={'forward_days':60,'mature_per_horizon':300,'closed_trades':50,
            'coverage':.8,'coverage_tolerance':.05,'confidence':.95,'stress_multiplier':1.5,
            'rollback_days':20,'rollback_drawdown_delta':.02}

def init():
    if db.q1("SELECT 1 FROM sqlite_master WHERE type='table' AND name='forecast_control_events'"):
        return
    if db.writes_frozen():
        return
    with db.tx() as c:
        c.executescript('''CREATE TABLE IF NOT EXISTS forecast_controls(name TEXT PRIMARY KEY,value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS forecast_evaluations(id INTEGER PRIMARY KEY AUTOINCREMENT,
          version INTEGER NOT NULL,kind TEXT NOT NULL,start TEXT NOT NULL,end TEXT NOT NULL,
          created TEXT NOT NULL,evidence_hash TEXT NOT NULL,report TEXT NOT NULL,
          UNIQUE(version,kind,start,end));
          CREATE TABLE IF NOT EXISTS forecast_control_events(id INTEGER PRIMARY KEY AUTOINCREMENT,
          created TEXT NOT NULL,action TEXT NOT NULL,previous TEXT NOT NULL);''')

def controls():
    init()
    exists=db.q1("SELECT 1 FROM sqlite_master WHERE name='forecast_controls'")
    values={r['name']:db.loads(r['value']) for r in db.q('SELECT * FROM forecast_controls')} if exists else {}
    return {'active_version':0,'previous_version':0,'paused':False,'adopted_at':None,
            'last_monitor_end':None,**values,'protocol':PROTOCOL,'thresholds':THRESHOLDS}

def _put(c,key,value):
    c.execute('INSERT OR REPLACE INTO forecast_controls VALUES(?,?)',(key,db.dumps(value)))

def control(action,asof=None):
    init(); t=stamp(asof) if asof else now()
    if action not in ('pause','resume','rollback') or not t:
        raise ValueError('invalid_control')
    with db.tx() as c:
        previous_state={r['name']:db.loads(r['value']) for r in c.execute('SELECT * FROM forecast_controls')}
        c.execute('INSERT INTO forecast_control_events(created,action,previous) VALUES(?,?,?)',
                  (t.isoformat(),action,db.dumps(previous_state)))
        if action=='rollback':
            current={r['name']:db.loads(r['value']) for r in c.execute('SELECT * FROM forecast_controls')}
            old=current.get('active_version',0); previous=current.get('previous_version',0)
            _put(c,'active_version',previous); _put(c,'previous_version',0)
            if old:
                c.execute("UPDATE forecast_models SET status='rolled_back' WHERE version=?",(old,))
            if previous:
                c.execute("UPDATE forecast_models SET status='active' WHERE version=?",(previous,))
            _put(c,'adopted_at',t.isoformat()); _put(c,'last_monitor_end',None)
        else:
            _put(c,'paused',action=='pause')
        _put(c,'last_control',{'action':action,'at':t.isoformat()})
    return controls()

def drawdown(returns):
    equity=peak=1.; worst=0.
    for r in returns:
        equity*=1+r; peak=max(peak,equity); worst=max(worst,1-equity/peak)
    return worst

def cumulative(returns):
    wealth=1.
    for r in returns:
        wealth*=1+r
    return wealth-1

def block_ci(excess,seed=1729,draws=2000):
    """Circular five-trading-day block bootstrap of paired daily excess mean."""
    if len(excess)<20 or any(not finite(x) for x in excess):
        return [None,None]
    rng=random.Random(seed); n=len(excess); means=[]
    for _ in range(draws):
        sample=[]
        while len(sample)<n:
            start=rng.randrange(n)
            sample.extend(excess[(start+k)%n] for k in range(5))
        means.append(sum(sample[:n])/n)
    means.sort()
    return [means[int(draws*.025)],means[min(draws-1,int(draws*.975))]]

def _daily(evidence):
    rows=evidence.get('daily',[])
    if not isinstance(rows,list):
        return [],False
    dates=set(); good=[]
    for r in rows:
        date=r.get('date'); t=stamp(str(date)+'T00:00:00')
        if (not t or t.weekday()>4 or date in dates or r.get('origin')!='forward' or
            any(not finite(r.get(k)) for k in ('candidate_return','baseline_return','candidate_cost','baseline_cost')) or
            min(r['candidate_cost'],r['baseline_cost'])<0 or min(r['candidate_return'],r['baseline_return'])<=-1):
            return [],False
        dates.add(date);good.append(r)
    return sorted(good,key=lambda r:r['date']),True

def evaluate(evidence):
    daily,valid=_daily(evidence); predictions=evidence.get('predictions',[])
    metrics={}; ids=set(); valid_predictions=isinstance(predictions,list)
    for horizon in HORIZONS:
        usable=[]
        for p in predictions if isinstance(predictions,list) else []:
            if p.get('horizon')!=horizon:
                continue
            if p.get('status')!='settled':
                continue  # Remains visible in missing/unknown counts; not a mature label.
            identity=p.get('id'); q=p.get('quantiles') or {}
            if (not identity or identity in ids or p.get('origin')!='forward' or
                any(not finite(p.get(k)) for k in ('outcome_return','probability','prior_probability')) or
                not finite(q.get('p10')) or not finite(q.get('p90')) or q['p10']>q['p90'] or
                not 0<=p['probability']<=1 or not 0<=p['prior_probability']<=1):
                valid_predictions=False; continue
            ids.add(identity); usable.append(p)
        n=len(usable)
        metrics[horizon]={'n':n,'brier':sum((p['probability']-int(p['outcome_return']>0))**2 for p in usable)/n if n else None,
                          'prior_brier':sum((p['prior_probability']-int(p['outcome_return']>0))**2 for p in usable)/n if n else None,
                          'coverage':sum(p['quantiles']['p10']<=p['outcome_return']<=p['quantiles']['p90'] for p in usable)/n if n else None}
    cand=[r['candidate_return'] for r in daily]; base=[r['baseline_return'] for r in daily]
    ci=block_ci([a-b for a,b in zip(cand,base)])
    exact_stress=bool(daily) and all(finite(r.get('candidate_stress_return')) and finite(r.get('baseline_stress_return'))
                  and min(r['candidate_stress_return'],r['baseline_stress_return'])>-1 for r in daily)
    stress=(cumulative([r['candidate_stress_return'] for r in daily])-cumulative([r['baseline_stress_return'] for r in daily])) if exact_stress else None
    trades=evidence.get('closed_trades'); counts=finite(trades) and trades>=50 and trades==int(trades)
    checks={
        'forward_complete':evidence.get('origin')=='forward' and evidence.get('complete') is True and valid and valid_predictions and
            all(finite(evidence.get(k)) and evidence[k]>=0 and int(evidence[k])==evidence[k] for k in ('missing','failed')),
        'forward_days':len(daily)>=60,
        'mature_horizons':all(m['n']>=300 for m in metrics.values()),
        'closed_trades':bool(counts),
        'probability':all(m['brier'] is not None and m['brier']<m['prior_brier'] for m in metrics.values()),
        'interval_coverage':all(m['coverage'] is not None and abs(m['coverage']-.8)<=.050000001 for m in metrics.values()),
        'excess_ci':ci[0] is not None and ci[0]>0,
        'drawdown':bool(daily) and drawdown(cand)<=drawdown(base),
        'stress':exact_stress and stress>0 and evidence.get('stress_infeasible') is not True,
    }
    return {'protocol':PROTOCOL,'thresholds':THRESHOLDS,'passed':all(checks.values()),'checks':checks,
            'ready':checks['forward_days'] and checks['mature_horizons'] and checks['closed_trades'] and checks['forward_complete'],
            'horizons':metrics,'days':len(daily),'closed_trades':trades,'excess_ci':ci,
            'candidate_drawdown':drawdown(cand),'baseline_drawdown':drawdown(base),'stress_excess':stress,
            'missing':evidence.get('missing'),'failed':evidence.get('failed'),
            'start':daily[0]['date'] if daily else None,'end':daily[-1]['date'] if daily else None}

def _record(c,version,kind,report,evidence,t):
    digest=hashlib.sha256(json.dumps(evidence,sort_keys=True,allow_nan=False).encode()).hexdigest()
    c.execute('INSERT INTO forecast_evaluations(version,kind,start,end,created,evidence_hash,report) VALUES(?,?,?,?,?,?,?)',
              (version,kind,report['start'],report['end'],t.isoformat(),digest,db.dumps(report)))

def decide(candidate_version,evidence,asof=None):
    init(); t=stamp(asof) if asof else now(); report=evaluate(evidence)
    if not t:
        raise ValueError('invalid_asof')
    if not report['ready']:
        return {**report,'decision':'waiting'}
    with db.tx() as c:
        row=c.execute('SELECT status,created,metrics FROM forecast_models WHERE version=?',(candidate_version,)).fetchone()
        if not row or row['status']!='candidate':
            return {**report,'decision':'not_candidate'}
        if evidence.get('model_version') != candidate_version:
            return {**report,'decision':'model_identity_missing'}
        settled=[p for p in evidence.get('predictions',[]) if p.get('status')=='settled']
        if any(p.get('model_version') != candidate_version or not stamp(p.get('asof')) or
               not stamp(p.get('due_at')) or not stamp(row['created']) <= stamp(p['asof']) < stamp(p['due_at']) <= t
               for p in settled):
            return {**report,'decision':'invalid_prediction_identity_or_time'}
        # Forward examination must start after this exact frozen candidate existed.
        if report['start']<=row['created'][:10] or report['end']>=t.date().isoformat():
            return {**report,'decision':'invalid_forward_window'}
        if c.execute('SELECT 1 FROM forecast_evaluations WHERE version=? AND kind=?',(candidate_version,'promotion')).fetchone():
            return {**report,'decision':'already_examined'}
        metrics=db.loads(row['metrics'])
        report['checks']['oos']=all(metrics.get(h+':fusion',{}).get('oos_passed') is True for h in HORIZONS)
        report['passed']=all(report['checks'].values())
        _record(c,candidate_version,'promotion',report,evidence,t)
        if report['passed']:
            prev=c.execute("SELECT value FROM forecast_controls WHERE name='active_version'").fetchone()
            previous=db.loads(prev[0]) if prev else 0
            if previous:
                c.execute("UPDATE forecast_models SET status='retired' WHERE version=?",(previous,))
            c.execute("UPDATE forecast_models SET status='active' WHERE version=?",(candidate_version,))
            for key,value in [('active_version',candidate_version),('previous_version',previous),('adopted_at',t.isoformat()),('last_monitor_end',None)]:
                _put(c,key,value)
            decision='adopted'
        else:
            c.execute("UPDATE forecast_models SET status='rejected' WHERE version=?",(candidate_version,)); decision='rejected'
    return {**report,'decision':decision}

def monitor(evidence,asof=None):
    state=controls(); t=stamp(asof) if asof else now()
    if not t:
        raise ValueError('invalid_asof')
    if evidence.get('complete') is not True or evidence.get('origin')!='forward':
        control('pause',asof); return {'decision':'paused_data'}
    if not state['active_version']:
        return {'decision':'legacy_active'}
    if evidence.get('model_version') != state['active_version']:
        return {'decision':'model_identity_missing'}
    daily,valid=_daily(evidence)
    if not valid:
        control('pause',asof); return {'decision':'paused_data'}
    boundary=max(str(state.get('adopted_at') or '')[:10],str(state.get('last_monitor_end') or ''))
    daily=[r for r in daily if boundary<r['date']<t.date().isoformat()][:20]
    if len(daily)<20:
        return {'decision':'waiting'}
    ci=block_ci([r['candidate_return']-r['baseline_return'] for r in daily])
    dd=drawdown([r['candidate_return'] for r in daily])-drawdown([r['baseline_return'] for r in daily])
    report={'start':daily[0]['date'],'end':daily[-1]['date'],'excess_ci':ci,'drawdown_delta':dd,
            'decision':'rollback' if ci[1]<0 or dd>.02 else 'retain'}
    with db.tx() as c:
        current=c.execute("SELECT value FROM forecast_controls WHERE name='active_version'").fetchone()
        last=c.execute("SELECT value FROM forecast_controls WHERE name='last_monitor_end'").fetchone()
        if not current or db.loads(current[0])!=state['active_version'] or (last and str(db.loads(last[0]) or '')>=report['end']):
            return {'decision':'stale_monitor'}
        _record(c,state['active_version'],'monitor',report,evidence,t)
        _put(c,'last_monitor_end',report['end'])
        if report['decision']=='rollback':
            c.execute("UPDATE forecast_models SET status='rolled_back' WHERE version=?",(state['active_version'],))
            if state['previous_version']:
                c.execute("UPDATE forecast_models SET status='active' WHERE version=?",(state['previous_version'],))
            _put(c,'active_version',state['previous_version']);_put(c,'previous_version',0)
            _put(c,'adopted_at',t.isoformat());_put(c,'last_monitor_end',None)
    return report

def view():
    state=controls()
    rows=db.q('SELECT id,version,kind,start,end,created,report FROM forecast_evaluations ORDER BY id DESC LIMIT 30') if db.q1("SELECT 1 FROM sqlite_master WHERE name='forecast_evaluations'") else []
    actions=db.q('SELECT id,created,action,previous FROM forecast_control_events ORDER BY id DESC LIMIT 30') if db.q1("SELECT 1 FROM sqlite_master WHERE name='forecast_control_events'") else []
    return {'controls':state,'evaluations':[{**r,'report':db.loads(r['report'])} for r in rows],
            'actions':[{**r,'previous':db.loads(r['previous'])} for r in actions]}
