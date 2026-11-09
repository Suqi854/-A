"""Cloud/local single-owner forecast lifecycle, isolated from quote acquisition."""
from __future__ import annotations

import hashlib
import json
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from .. import db, market
from ..config import MOCK, load_settings, now
from . import calendar, features, data, ledger, models, evaluation

_stop=threading.Event()
_threads=[]
_quotes={}
_lock=threading.RLock()
_queue=queue.Queue(maxsize=20)
_started=False
_cache={}
_due={}
STATE={'running':False,'phase':'not_started','last_prediction':None,'last_execution':None,'error':None,
       'covered':0,'requested':0,'excluded':[],'dropped_quote_batches':0,'code_status':{},'model_training':False}


def init():
    with db.tx() as c:
        c.executescript('''CREATE TABLE IF NOT EXISTS forecast_runtime(key TEXT PRIMARY KEY,value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS forecast_structures(id TEXT PRIMARY KEY,code TEXT NOT NULL,
          timeframe TEXT NOT NULL,asof TEXT NOT NULL,body TEXT NOT NULL);
          CREATE INDEX IF NOT EXISTS ix_forecast_structures ON forecast_structures(code,timeframe,asof);
          CREATE TABLE IF NOT EXISTS forecast_decisions(id TEXT PRIMARY KEY,code TEXT NOT NULL,book TEXT NOT NULL,
          at TEXT NOT NULL,action TEXT NOT NULL,reason TEXT NOT NULL,prediction_id TEXT,body TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS forecast_jobs(id TEXT PRIMARY KEY,at TEXT NOT NULL,status TEXT NOT NULL,body TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS forecast_ai_orders(id TEXT PRIMARY KEY,code TEXT NOT NULL,side TEXT NOT NULL,
          qty INTEGER NOT NULL,submitted_at TEXT NOT NULL,source_time TEXT NOT NULL,source TEXT NOT NULL,
          expires_at TEXT NOT NULL,version INTEGER NOT NULL,status TEXT NOT NULL,body TEXT NOT NULL,order_id INTEGER);
          CREATE TABLE IF NOT EXISTS forecast_ai_cycles(id INTEGER PRIMARY KEY,code TEXT NOT NULL,qty INTEGER NOT NULL,
          remaining INTEGER NOT NULL,opened_at TEXT NOT NULL,side TEXT NOT NULL DEFAULT 'sell');
          CREATE TABLE IF NOT EXISTS forecast_ai_applied_trades(id INTEGER PRIMARY KEY);''')


def cfg():
    return {'enabled':True,'max_codes':40,'max_candidates':20,'max_positions':12,'position_fraction':.12,
            'buy_probability':.55,'sell_probability':.40,'min_expected_return':.004,
            **load_settings().get('forecast',{})}


def _set(key,value):
    with db.tx() as c:
        c.execute('INSERT OR REPLACE INTO forecast_runtime VALUES(?,?)',(key,db.dumps(value)))


def _get(key,default=None):
    row=db.q1('SELECT value FROM forecast_runtime WHERE key=?',(key,))
    return db.loads(row['value'],default) if row else default


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,allow_nan=False).encode()).hexdigest()


def universe():
    from .. import picker
    positions=[r['code'] for r in db.q('SELECT DISTINCT code FROM positions ORDER BY account,code')]
    if db.q1("SELECT 1 FROM sqlite_master WHERE name='forecast_books'"):
        for r in db.q('SELECT holdings,baseline FROM forecast_books'):
            positions+=list(db.loads(r['holdings'],{}))
            positions+=list(db.loads(r['baseline'],{}).get('quantities',{}))
    s=load_settings(); recent=picker.runs(1)
    candidates=[p['code'] for p in recent[0].get('items',[])] if recent else []
    ordered=list(dict.fromkeys(positions+s.get('watchlist',[])+candidates[:cfg()['max_candidates']]))
    allowed=[market.norm_code(c) for c in ordered if market.is_main_board(market.norm_code(c))]
    cap=min(40,max(1,int(cfg()['max_codes'])))
    selected=allowed[:cap]
    STATE.update(covered=len(selected),requested=len(ordered),excluded=[c for c in ordered if c not in selected])
    return selected


def feed(quotes,asof=None):
    """Nonblocking boundary called by the existing quote loop; no database work."""
    if not _started or _stop.is_set():
        return
    t=calendar.iso(asof or now())
    with _lock:
        _quotes.update({k:dict(v) for k,v in quotes.items()})
        snapshot=dict(_quotes)
    try:
        _queue.put_nowait((snapshot,t))
    except queue.Full:
        STATE['dropped_quote_batches']+=1


def _structure(code,tf,t=None):
    from . import chan
    bars=data.fetch(code,tf,t)
    t=calendar.stamp(t) if t else now()
    signature=digest([bars.get('source'),bars.get('adjustment'),[{k:r.get(k) for k in ('date','open','high','low','close','vol')} for r in bars['rows']]])
    key=(code,tf)
    cached=_cache.get(key)
    if cached and cached['signature']==signature:
        result=cached['result']
    else:
        result=chan.analyze(code,tf,bars['rows'],calendar.iso(t))
        _cache[key]={'signature':signature,'result':result}
        with db.tx() as c:
            c.execute('INSERT OR IGNORE INTO forecast_structures VALUES(?,?,?,?,?)',
                      (result['id'],code,tf,result['asof'],db.dumps(result)))
    return bars,result


def process_code(code,asof=None):
    """Potentially slow K-line/Chan work; runs only in the bounded research pool."""
    t=calendar.stamp(asof or now()); frames={}; structures={}; failures={}
    for tf in ('day','60m','15m','5m'):
        if _stop.is_set():
            return {'stopped':True}
        try:
            frames[tf],structures[tf]=_structure(code,tf,t if asof is not None else None)
            if not frames[tf]['quality']['eligible']:
                failures[tf]=frames[tf]['quality']['reason']
        except (RuntimeError,ValueError,TypeError,KeyError,OSError) as exc:
            failures[tf]=type(exc).__name__
    # Use the current observed time, not a timestamp captured before network calls.
    context=data.context(t if asof is not None else now(),code=code)
    t=calendar.stamp(asof) if asof else now()
    with _lock:
        quote=dict(_quotes.get(code) or {})
    qtime=features.stamp(quote.get('source_time') or quote.get('time'))
    fresh=bool(qtime and 0<=(t-qtime).total_seconds()<=12 and quote.get('source') and quote.get('price',0)>0)
    if not fresh:
        failures['quote']='missing_or_stale_quote'
    f=features.build(frames.get('day',{}).get('rows',[]),asof=t,chan=structures,quote=quote,
                     context=context,disclosure=data.disclosure(code,t))
    _set('features:'+code,{'coverage':f['coverage'],'version':f['version'],'hash':f['hash'],'at':calendar.iso(t)})
    from . import chan
    long_align=chan.align_levels([structures.get(k,{}) for k in ('day','60m')])
    short_align=chan.align_levels([structures.get(k,{}) for k in ('15m','5m')])
    issued=[]
    if fresh and calendar.in_session(t,calendar.official_days(t.year)):
        for horizon in models.HORIZONS:
            group=('15m','5m') if horizon.endswith('m') else ('day','60m')
            due=calendar.due_at(t,horizon,calendar.calendar_window(t.year))
            if not due:
                failures[horizon]='insufficient_session_or_unknown_calendar';continue
            alignment=short_align if horizon.endswith('m') else long_align
            good=all(k in frames and frames[k]['quality']['eligible'] and structures[k]['quality']['eligible']
                     and structures[k].get('confirmed') for k in group) and alignment.get('eligible',False)
            quality={'eligible':bool(good),'reason':None if good else 'incomplete_structure_inputs'}
            # Stable per-code/horizon completed bar slot; restart does not create new origins.
            slot=frames.get(group[-1],{}).get('rows',[])
            slot=slot[-1]['date'] if slot else calendar.iso(t.replace(second=0,microsecond=0))
            versions=[r['version'] for r in db.q("SELECT version FROM forecast_models WHERE status IN ('candidate','active') ORDER BY version")]
            if not versions:versions=[0]
            for variant,version in ((variant,version) for variant in models.VARIANTS for version in versions):
                forecast=models.predict(f['values'],horizon,variant,model_version=version)
                identity=digest([code,horizon,variant,forecast.get('model_version'),slot])
                if db.q1('SELECT id FROM forecast_predictions WHERE id=?',(identity,)):
                    continue
                record={'id':identity,'code':code,'horizon':horizon,'asof':calendar.iso(t),'due_at':due,
                        'reference_price':quote['price'],'features':f['values'],'feature_hash':f['hash'],'feature_version':f['version'],
                        'structure_id':digest([structures.get(k,{}).get('id') for k in group]),
                        'structure_ids':[structures.get(k,{}).get('id') for k in group],
                        **forecast,'variant':variant,'quality':quality,'source_time':calendar.iso(qtime),
                        'origin':'synthetic' if MOCK else 'forward','alignment':alignment,
                        'long_alignment':long_align,'short_alignment':short_align,
                        'invalidation':_invalidation(structures.get('day',{}),quote['price']),
                        'coverage':f['coverage']}
                ledger.record_prediction(record);issued.append(identity)
    status={'at':calendar.iso(t),'failures':failures,'long':long_align,'short':short_align,
            'issued':len(issued),'coverage':f['coverage']}
    with _lock:
        STATE['code_status'][code]=status
        if issued:STATE['last_prediction']=calendar.iso(t)
    _set('code:'+code,status)
    return status


def _invalidation(structure,price):
    centers=[x for x in structure.get('structures',{}).get('centers',[]) if x.get('confirmed')]
    # Risk cap remains an explicit price boundary even when the center is absent.
    cap=float(load_settings().get('risk',{}).get('max_stop_pct',.08))
    floor=price*(1-min(.08,max(.01,cap)))
    for c in reversed(centers):
        low=c.get('low') or c.get('ZD')
        if features.finite(low) and floor<low<price:
            return low
    return floor


def _opening(quotes,t):
    if db.q1('SELECT 1 FROM forecast_books LIMIT 1'):
        return True
    acct=db.q1("SELECT * FROM accounts WHERE id='plan'") or db.q1("SELECT * FROM accounts WHERE id='ai'")
    if not acct:
        STATE['phase']='awaiting_account';return False
    positions=db.q('SELECT code,qty,cost,last_buy,stop,target1,target2,source,auto,stage,opened FROM positions WHERE account=?',(acct['id'],))
    try:
        ledger.initialize(acct['cash'],positions,quotes,t,fees=load_settings()['fees'],slippage=load_settings()['slippage'])
        _set('opening_account',acct['id'])
        _set('policy',{'version':'decision-v1','config':cfg(),'legacy_settings':load_settings(),'legacy_positions':positions,
                       'rule':'long_filter_short_timing','created':calendar.iso(t)})
        return True
    except ValueError as exc:
        STATE['phase']='awaiting_valid_opening';STATE['error']=str(exc);return False


def _note(book,code,action,reason,prediction,t,body=None):
    identity=digest([book,code,action,reason,prediction])
    with db.tx() as c:
        c.execute('INSERT OR IGNORE INTO forecast_decisions VALUES(?,?,?,?,?,?,?,?)',
                  (identity,code,book,calendar.iso(t),action,reason,prediction,db.dumps(body or {})))


def decision(long,short,book,code,quote,t,*,rule=False):
    """Deterministic policy. Uncalibrated math cannot masquerade as an executable edge."""
    holding=book.get('holdings',{}).get(code,{});qty=holding.get('qty',0)
    available=ledger._available(holding,t)
    if not long or not long.get('quality',{}).get('eligible'):
        return 'wait',0,'long_data_missing','position'
    if calendar.stamp(long['due_at'])<=t:
        return 'wait',0,'prediction_expired','position'
    px=quote['price']; stop=long.get('invalidation')
    if qty and features.finite(stop) and px<=stop:
        return ('sell',available//100*100,'long_risk_exit','position') if available>=100 else ('wait',0,'t1_locked_risk','position')
    if not short or not short.get('quality',{}).get('eligible') or calendar.stamp(short['due_at'])<=t:
        return 'wait',0,'short_data_missing','position'
    if long.get('alignment',{}).get('state')=='conflict' or short.get('alignment',{}).get('state')=='conflict':
        return 'wait',0,'timeframe_conflict','position'
    long_direction=long.get('alignment',{}).get('direction',0)
    short_direction=short.get('alignment',{}).get('direction',0)
    if long_direction and short_direction and long_direction!=short_direction:
        return 'wait',0,'long_short_conflict','position'
    c=cfg();lp,sp=long.get('probability'),short.get('probability')
    if rule:
        vals=short.get('features',{})
        buy_points=[v for k,v in vals.items() if k.startswith('chan_5m_') and k.endswith('buy_point')]
        sell_points=[v for k,v in vals.items() if k.startswith('chan_5m_') and k.endswith('sell_point')]
        ages=[v for k,v in vals.items() if k.startswith('chan_5m_') and k.endswith('point_confirmation_age_bars')]
        if not (buy_points or sell_points) or not ages or min(ages)>3:
            return 'wait',0,'no_recent_confirmed_chan_point','position'
        buy=long.get('alignment',{}).get('direction')==1 and any(v>0 for v in buy_points)
        sell=any(v>0 for v in sell_points)
    else:
        if not long.get('validated') or not short.get('validated') or lp is None or sp is None:
            return 'wait',0,'model_unvalidated','position'
        buy=lp>=c['buy_probability'] and sp>=c['buy_probability'] and (long.get('quantiles',{}).get('p50') or 0)>c['min_expected_return']
        sell=lp<c['sell_probability'] or sp<c['sell_probability']
    cycles=[x for x in book.get('open_cycles',[]) if x['code']==code and x.get('side')=='sell']
    if cycles:
        if buy:
            return 'buy',sum(x['remaining'] for x in cycles),'t_buyback','t'
        return 'wait',0,'open_t_cycle_priority','t'
    if sell and available>=100:
        is_t=not rule and lp>=c['buy_probability']
        return 'sell',(max(100,available//300*100) if is_t else available//100*100),'short_sell' if is_t else 'long_sell','t' if is_t else 'position'
    if buy and not qty and len(book.get('holdings',{}))<c['max_positions']:
        equity=book.get('baseline',{}).get('equity_fen',0)/100
        budget=min(book.get('cash',0)*.98,equity*c['position_fraction'])
        return 'buy',max(0,int(budget/(px*1.005)/100)*100),'long_short_entry','position'
    return 'wait',0,'waiting_signal','position'


def latest_predictions():
    from .archive import read_record
    rows=db.q('''SELECT body FROM (SELECT body,ROW_NUMBER() OVER(
      PARTITION BY code,variant,horizon,model_version ORDER BY asof DESC,id DESC) AS rank
      FROM forecast_predictions) WHERE rank=1''')
    return [read_record(r['body']) for r in rows]


def _paper_decisions(quotes,t):
    books=ledger.view().get('books',{})
    rows=latest_predictions()
    latest={}
    for p in rows:
        latest.setdefault((p['variant'],p['code'],p['horizon'],str(p.get('model_version') or 0)),p)
    for book,state in books.items():
        variant=book.split('@')[0]
        if variant not in models.VARIANTS:continue
        version=book.split('@')[1] if '@' in book else '0'
        if version!='0' and not db.q1("SELECT 1 FROM forecast_models WHERE version=? AND status IN ('candidate','active')",(version,)):continue
        if not state:continue
        for code,q in quotes.items():
            clean,error=ledger._quote(q,t,require_limits=True)
            if error:continue
            long=latest.get((variant,code,'3d',version)) or latest.get((variant,code,'1d',version))
            short=latest.get((variant,code,'30m',version))
            action,qty,why,intent=decision(long,short,state,code,q,t,rule=variant=='chan_only')
            p=long if why in ('long_risk_exit','long_sell') else short or long
            pid=p['id'] if p else 'missing:'+code+':'+t.date().isoformat()
            _note(book,code,action,why,pid,t)
            if action!='wait' and qty>=100 and p:
                reserved={r['code'] for r in db.q("SELECT DISTINCT code FROM forecast_orders WHERE book=? AND side='buy' AND status='pending'",(book,))}
                if action=='buy' and code not in state.get('holdings',{}) and len(set(state.get('holdings',{}))|reserved)>=(cfg()['max_positions']):
                    _note(book,code,'blocked','position_slots_reserved',pid,t);continue
                try:
                    ledger.submit(book,code,action,qty,pid,q,t,reason=why,intent=intent,
                                  expires_at=calendar.iso(min(t+timedelta(minutes=5),calendar.stamp(p['due_at']))))
                except ValueError as exc:
                    _note(book,code,'blocked',str(exc),pid,t)


def owns_ai():
    try:
        row=db.q1("SELECT value FROM forecast_controls WHERE name='active_version'")
        return bool(row and int(db.loads(row['value'],0)))
    except Exception:
        return False


def _compatible_ai():
    a=db.q1("SELECT cash FROM accounts WHERE id='ai'")
    return bool(a and a['cash']>=0 and not db.q1("SELECT 1 FROM positions WHERE account='ai' AND qty<0"))


def _update_ai_cycles():
    """Apply actual fills exactly once; a submitted T leg is not a completed T leg."""
    with db.tx() as c:
        rows=c.execute('''SELECT t.*,f.body FROM trades t JOIN forecast_ai_orders f ON f.order_id=t.order_id
          LEFT JOIN forecast_ai_applied_trades a ON a.id=t.id WHERE a.id IS NULL ORDER BY t.id''').fetchall()
        for r in rows:
            body=db.loads(r['body'],{})
            if body.get('intent')=='t':
                if r['side']=='sell':
                    c.execute("INSERT OR IGNORE INTO forecast_ai_cycles(id,code,qty,remaining,opened_at) VALUES(?,?,?,?,?)",
                              (r['id'],r['code'],r['qty'],r['qty'],r['time']))
                else:
                    remaining=r['qty']
                    for cycle in c.execute('SELECT * FROM forecast_ai_cycles WHERE code=? AND remaining>0 ORDER BY id',(r['code'],)).fetchall():
                        used=min(remaining,cycle['remaining'])
                        c.execute('UPDATE forecast_ai_cycles SET remaining=remaining-? WHERE id=?',(used,cycle['id']))
                        remaining-=used
                        if not remaining:break
            c.execute('INSERT OR IGNORE INTO forecast_ai_applied_trades VALUES(?)',(r['id'],))


def _ai_decisions(quotes,t):
    """Adopted policy stages intents first; only a later quote may execute them."""
    _update_ai_cycles()
    control=evaluation.controls()
    version=control['active_version']
    if not version or control['paused'] or not _compatible_ai():
        return
    from .. import sim
    a=db.q1("SELECT * FROM accounts WHERE id='ai'")
    positions=db.q("SELECT * FROM positions WHERE account='ai'")
    held={}
    for p in positions:
        available=sim.available('ai',p['code'])
        held[p['code']]={'qty':p['qty'],'lots':[{'qty':available,'acquired_at':calendar.iso(t-timedelta(days=1))},
                                              {'qty':p['qty']-available,'acquired_at':calendar.iso(t)}]}
    book={'cash':a['cash'],'baseline':{'equity_fen':round(a['cash']*100)+sum(round(quotes.get(p['code'],{}).get('price',p['cost'])*p['qty']*100) for p in positions)},
          'holdings':held,'open_cycles':db.q('SELECT * FROM forecast_ai_cycles WHERE remaining>0')}
    reserved={r['code'] for r in db.q("SELECT DISTINCT code FROM forecast_ai_orders WHERE side='buy' AND status='pending'")}
    for code,q in quotes.items():
        clean,error=ledger._quote(q,t,require_limits=True)
        if error:continue
        records=[p for p in latest_predictions() if p['code']==code]
        chosen={}
        for p in records:
            if p['variant']=='fusion' and str(p.get('model_version'))==str(version):chosen.setdefault(p['horizon'],p)
        long,short=chosen.get('3d') or chosen.get('1d'),chosen.get('30m')
        action,qty,why,intent=decision(long,short,book,code,q,t)
        if action=='wait' or qty<100:continue
        if action=='buy' and code not in held and len(set(held)|reserved)>=cfg()['max_positions']:continue
        p=long if why in ('long_risk_exit','long_sell') else short or long
        identity=digest(['ai',version,action,p['id']])
        if db.q1("SELECT 1 FROM forecast_ai_orders WHERE code=? AND status='pending'",(code,)) or db.q1("SELECT 1 FROM orders WHERE account='ai' AND code=? AND status='pending'",(code,)):
            continue
        with db.tx() as c:
            c.execute('INSERT OR IGNORE INTO forecast_ai_orders VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL)',
                      (identity,code,action,qty,calendar.iso(t),clean['time'],clean['source'],
                       calendar.iso(t+timedelta(minutes=5)),version,'pending',db.dumps({'reason':why,'intent':intent,'prediction_id':p['id'],
                       'stop':long.get('invalidation') if long else None,'zone':[q['price']*.995,q['price']*1.005]})))
        if action=='buy':reserved.add(code)
    for o in db.q("SELECT * FROM forecast_ai_orders WHERE status='pending' ORDER BY submitted_at"):
        # An interrupted process may have placed the durable legacy order already.
        marker='forecast:'+o['id']
        existing=db.q1("SELECT id FROM orders WHERE account='ai' AND reason=?",(marker,))
        if existing:
            with db.tx() as c:c.execute("UPDATE forecast_ai_orders SET status='submitted',order_id=? WHERE id=?",(existing['id'],o['id']))
            continue
        q=quotes.get(o['code']); clean,error=ledger._quote(q,t,require_limits=True)
        if o['version']!=version:
            with db.tx() as c:c.execute("UPDATE forecast_ai_orders SET status='cancelled' WHERE id=?",(o['id'],))
            continue
        if calendar.stamp(o['expires_at'])<=t:
            with db.tx() as c:c.execute("UPDATE forecast_ai_orders SET status='expired' WHERE id=?",(o['id'],))
            continue
        if error or clean['source']!=o['source'] or calendar.stamp(clean['time'])<=max(calendar.stamp(o['source_time']),calendar.stamp(o['submitted_at'])):
            continue
        body=db.loads(o['body']);px=q['price']
        if o['side']=='buy':
            latest={}
            for p in latest_predictions():
                if p['code']==o['code'] and p['variant']=='fusion' and str(p.get('model_version'))==str(version):latest.setdefault(p['horizon'],p)
            long=latest.get('3d') or latest.get('1d');short=latest.get('30m')
            if any(not p or not p.get('quality',{}).get('eligible') or calendar.stamp(p['due_at'])<=t for p in (long,short)):
                with db.tx() as c:c.execute("UPDATE forecast_ai_orders SET status='cancelled' WHERE id=?",(o['id'],))
                continue
            current_action,current_qty,_,current_intent=decision(long,short,book,o['code'],q,t)
            if current_action!='buy' or current_qty<o['qty'] or current_intent!=body.get('intent','position'):
                with db.tx() as c:c.execute("UPDATE forecast_ai_orders SET status='cancelled' WHERE id=?",(o['id'],))
                continue
        if not body['zone'][0]<=px<=body['zone'][1]:continue
        if (o['side']=='buy' and px>=clean['upper']) or (o['side']=='sell' and px<=clean['lower']):continue
        cash=db.q1("SELECT cash FROM accounts WHERE id='ai'")['cash']
        total=px*(1+load_settings()['slippage'])*o['qty']
        if (o['side']=='buy' and total+sim.fees('buy',total)>cash) or (o['side']=='sell' and o['qty']>sim.available('ai',o['code'])):
            with db.tx() as c:c.execute("UPDATE forecast_ai_orders SET status='rejected' WHERE id=?",(o['id'],))
            continue
        try:
            order=sim.place('ai',o['code'],o['side'],o['qty'],qd=q,reason=marker,emotion='计划内',stop=body.get('stop'))
            with db.tx() as c:c.execute("UPDATE forecast_ai_orders SET status='submitted',order_id=? WHERE id=?",(order['id'],o['id']))
        except ValueError as exc:
            with db.tx() as c:c.execute("UPDATE forecast_ai_orders SET status='rejected',body=? WHERE id=?",(db.dumps({**body,'error':str(exc)}),o['id']))


def execute(quotes,asof):
    t=calendar.stamp(asof)
    if not t:return
    ledger.settle(quotes,t)
    if not cfg()['enabled']:return
    if not _opening(quotes,t):return
    for row in db.q("SELECT version FROM forecast_models WHERE status IN ('candidate','active')"):
        try:ledger.initialize_version(row['version'],quotes,t)
        except ValueError:pass  # Wait for one complete, same-price opening valuation.
    control=evaluation.controls()
    if calendar.in_session(t,calendar.official_days(t.year)):
        if not control['paused']:
            ledger.match(quotes,t)
            _paper_decisions(quotes,t)
            _ai_decisions(quotes,t)
            from . import legacy
            for book in ledger.view().get('books',{}):
                if book.split('@')[0]=='legacy_control':legacy.tick(quotes,t,book=book)
        fresh=any(ledger._quote(q,t)[0] for q in quotes.values())
        analyzed=list(STATE['code_status'].values())
        all_bad=bool(analyzed) and all(any(tf in r.get('failures',{}) for tf in ('day','60m','15m','5m','quote')) for r in analyzed)
        STATE['phase']='paused' if control['paused'] else 'data_blocked' if not fresh or all_bad else 'waiting_signal'
    elif t.hour==15 and t.minute<10:
        ledger.mark(quotes,t)
    STATE['last_execution']=calendar.iso(t)


def maintenance(asof=None):
    t=calendar.stamp(asof or now())
    key='maintenance:'+t.date().isoformat()
    if db.q1('SELECT 1 FROM forecast_jobs WHERE id=?',(key,)):return
    STATE['model_training']=True
    try:
        from . import archive
        archived=archive.compact(asof=t)
        week=f'{t.isocalendar().year}-{t.isocalendar().week}'
        if _get('calendar_week')!=week:
            calendar.refresh_calendar(t.year,asof=t)
            calendar.refresh_calendar(t.year+1,asof=t)
            _set('calendar_week',week)
        result=models.train_candidate(ledger.training_rows(),asof=t)
        candidates=db.q("SELECT version FROM forecast_models WHERE status='candidate' ORDER BY version DESC LIMIT 1")
        reports=[]
        for row in candidates:
            evidence=ledger.evaluation_data(model_version=row['version'])
            if _compatible_ai():reports.append(evaluation.decide(row['version'],evidence,asof=t))
        active=evaluation.controls()['active_version']
        if active:
            reports.append(evaluation.monitor(ledger.evaluation_data(model_version=active),asof=t))
        with db.tx() as c:
            c.execute('INSERT OR IGNORE INTO forecast_jobs VALUES(?,?,?,?)',(key,calendar.iso(t),'done',db.dumps({'training':result,'evaluation':reports,'archive':archived})))
    finally:
        STATE['model_training']=False


def _research_loop():
    pending={}
    with ThreadPoolExecutor(max_workers=2,thread_name_prefix='forecast-research') as pool:
        while not _stop.is_set():
            t=now()
            if cfg()['enabled'] and t.date().isoformat() in calendar.official_days(t.year):
                if calendar.in_session(t,calendar.official_days(t.year)):
                    codes=universe()
                    for code in list(pending):
                        if pending[code].done():
                            try:pending[code].result()
                            except Exception as exc:STATE['error']=type(exc).__name__
                            del pending[code]
                    for code in codes:
                        if code not in pending and _due.get(code,0)<=time.time() and len(pending)<4:
                            pending[code]=pool.submit(process_code,code)
                            _due[code]=time.time()+60
                elif t.hour>=15 and t.minute>=10:
                    try:maintenance(t)
                    except Exception as exc:STATE['error']=type(exc).__name__
            _stop.wait(5)


def _execution_loop():
    last_close=0
    while not _stop.is_set():
        try:
            quotes,t=_queue.get(timeout=1)
            execute(quotes,now())
        except queue.Empty:
            pass
        except Exception as exc:
            STATE['error']=type(exc).__name__
        t=now()
        if t.hour==15 and t.minute<5 and time.time()-last_close>3:
            last_close=time.time()
            try:
                qs=market.quotes_fast(universe());execute(qs,now())
            except Exception as exc:STATE['error']='close:'+type(exc).__name__
        if not db.writes_frozen():
            try:_set('status',{**STATE,'asof':calendar.iso(t),'role':'cloud' if __import__('os').environ.get('AQ5_CLOUD')=='1' else 'pc'})
            except db.StateFrozen:break


def start():
    global _started
    if _started or not cfg()['enabled']:return
    _started=True;_stop.clear();STATE.update(running=True,phase='warming_up')
    for target in (_research_loop,_execution_loop):
        worker=threading.Thread(target=target,daemon=True,name='aq5-'+target.__name__)
        _threads.append(worker);worker.start()


def stop(timeout=180):
    _stop.set();deadline=time.monotonic()+timeout
    for worker in _threads:worker.join(max(0,deadline-time.monotonic()))
    if any(w.is_alive() for w in _threads):
        raise RuntimeError('forecast workers still writing; handoff not finalized')
    STATE['running']=False


def view(code=None,limit=100,compact=False):
    from .archive import read_record
    state=dict(STATE) if STATE['running'] else (_get('status',STATE))
    records=ledger.predictions(min(int(limit),150),code)
    records=[{k:v for k,v in p.items() if k not in ('features','coverage')} for p in records]
    sql='SELECT body FROM forecast_structures'+(' WHERE code=?' if code else '')+' ORDER BY asof DESC LIMIT 160'
    structs={}
    for r in db.q(sql,(code,) if code else ()):
        item=read_record(r['body']);key=item['code']+':'+item['timeframe']
        if key in structs:continue
        geometry=item.get('structures',{})
        small={k:v[-(90 if k=='klines' else 12):] for k,v in geometry.items() if k!='revisions'}
        structs[key]={k:item.get(k) for k in ('id','code','timeframe','asof','confirmed','quality','config_hash')}
        structs[key]['structures']=small
    coverage=[{'code':r['key'].split(':',1)[1],**db.loads(r['value'])} for r in db.q("SELECT key,value FROM forecast_runtime WHERE key LIKE 'features:%'")]
    books=ledger.view()
    books.pop('predictions',None)  # Full feature vectors stay in the immutable ledger, not every live packet.
    if compact:
        state={k:v for k,v in state.items() if k!='code_status'}
        for b in books.get('books',{}).values():
            b['orders']=[{k:o.get(k) for k in ('id','code','side','qty','status','reason','submitted_at','fill_time')} for o in b.get('orders',[])[:8]]
    return {'version':'forecast-1','status':state,'controls':evaluation.controls(),'predictions':records,
            'structures':structs,'coverage':coverage,'ledger':books,
            'decisions':db.q('SELECT code,book,at,action,reason FROM forecast_decisions ORDER BY at DESC LIMIT 60'),
            'calendar':calendar.status(),
            'model_status':models.view(),'evaluation':evaluation.view()}
