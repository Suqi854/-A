"""Run the original policy against the isolated T+1 comparison book.

No global monkeypatching: hf.tick and sim.auto_follow accept an explicit context.
Signals are unchanged; execution uses the same ledger constraints as challengers.
"""
from __future__ import annotations
import hashlib, json, threading
from collections import deque
from datetime import timedelta
from .. import db, hf, sim, marketstate
from ..config import now
from . import ledger
from .calendar import iso, stamp, in_session, official_days

BOOK='legacy_control'
_lock=threading.RLock()

def init():
    if db.q1("SELECT 1 FROM sqlite_master WHERE name='forecast_legacy_state'"):
        return
    if db.writes_frozen():
        return
    with db.tx() as c:
        c.execute('CREATE TABLE IF NOT EXISTS forecast_legacy_state(id TEXT PRIMARY KEY,body TEXT NOT NULL)')

class Context:
    def __init__(self,quotes,t,settings=None,market_context=None,book=BOOK):
        from . import runtime
        init();self.book=book;self.t=stamp(t) if not hasattr(t,'tzinfo') else t
        if not self.t:raise ValueError('invalid legacy time')
        self.settings=settings if settings is not None else (runtime._get('policy') or {}).get('legacy_settings',{})
        self.raw_quotes={c:dict(q) for c,q in quotes.items() if ledger._quote(q,self.t)[0]}
        self.backdrop=market_context
        row=db.q1('SELECT body FROM forecast_legacy_state WHERE id=?',(self.book,))
        saved=db.loads(row['body'],{}) if row else {}
        parent=None
        if not row and '@' in self.book:
            parent=db.q1('SELECT body FROM forecast_legacy_state WHERE id=?',(BOOK,))
        inherited=db.loads(parent['body'],{}) if parent else {}
        policy=runtime._get('policy') or {} if db.q1("SELECT 1 FROM sqlite_master WHERE name='forecast_runtime'") else {}
        self.initial=saved.get('initial') or inherited.get('positions_snapshot') or {p['code']:dict(p) for p in policy.get('legacy_positions',[])}
        self.order_terms=saved.get('order_terms',{})
        self.buffers={c:deque((x for x in xs if self.clock()-x[0]<=1200),maxlen=400) for c,xs in saved.get('buffers',{}).items()}
        self.sources=saved.get('sources',{});self.best=saved.get('best',{});self.cool=saved.get('cool',{})
        self.state=saved.get('state',{'ticks':0,'log':[]});self.simulator=self
        self.long,self.short=([dict(x) for x in hf._scan[k]] for k in ('long','short'))

    def now(self):return self.t
    def clock(self):return self.t.timestamp()
    def config(self):
        return hf.cfg(self.settings)
    def log(self,message):self.state['log']=([iso(self.t)+' '+str(message)]+self.state.get('log',[]))[:40]
    def candidates(self):return self.long,self.short
    def quotes(self,codes):return {c:self.raw_quotes[c] for c in codes if c in self.raw_quotes}
    def market_state(self):
        if self.backdrop is not None:
            return self.backdrop
        cached=dict(marketstate._cache)
        if cached.get('data') and 0<=self.clock()-cached.get('t',0)<60:
            return dict(cached['data'])
        self.gap('legacy_market_context_missing_or_stale')
        # hf.tick historically catches exceptions and defaults to risk_on; return
        # an explicit defensive backdrop so missing evidence cannot open a trade.
        return {'risk_off':True,'stance':'防守','data_gap':True}
    def ai_rules(self):return {'short':False,'margin':False,'t0':False,'max_leverage':1.}
    def orders_today(self):
        return db.q1('SELECT COUNT(*) AS n FROM forecast_orders WHERE book=? AND submitted_at LIKE ?',
                     (self.book,self.t.date().isoformat()+'%'))['n']
    def pending(self):return {r['code'] for r in db.q("SELECT code FROM forecast_orders WHERE book=? AND status='pending'",(self.book,))}
    def _account(self):
        row=db.q1('SELECT * FROM forecast_books WHERE id=?',(self.book,))
        if not row:raise ValueError('legacy comparison book not initialized')
        return row
    def positions(self):
        account=self._account();holdings=db.loads(account['holdings'],{});baseline=db.loads(account['baseline'],{})
        costs={c:{**self.initial.get(c,{}),'qty':q,'cost':self.initial.get(c,{}).get('cost') or baseline['prices'][c],
                  'source':self.initial.get(c,{}).get('source','frozen opening')} for c,q in baseline.get('quantities',{}).items()}
        fills=db.q('SELECT f.*,o.reason FROM forecast_fills f JOIN forecast_orders o ON o.id=f.order_id WHERE f.book=? ORDER BY f.time,f.order_id',(self.book,))
        for f in fills:
            p=costs.setdefault(f['code'],{'qty':0,'cost':0.,'source':''})
            if f['side']=='buy':
                p['cost']=(p['cost']*p['qty']+f['price_fen']/100*f['qty'])/(p['qty']+f['qty'])
                p['qty']+=f['qty'];p['source']=f['reason']
                p.update({k:v for k,v in self.order_terms.get(f['order_id'],{}).items() if v is not None})
            else:
                p['qty']=max(0,p['qty']-f['qty'])
                if '第一止盈' in f['reason']:
                    p['stage']=1;p['stop']=round(max(p['cost'],p.get('stop') or 0),2)
        result={}
        for code,p in holdings.items():
            cost=costs.get(code,{});q=self.raw_quotes.get(code,{})
            first=min((lot['acquired_at'] for lot in p.get('lots',[])),default=account['opened_at'])
            result[code]={**cost,'account':'ai','auto':cost.get('auto',1),'stage':cost.get('stage',0),'opened':cost.get('opened',first),
                          'code':code,'qty':p['qty'],'cost':cost.get('cost',0.),'source':cost.get('source',''),
                          'avail':ledger._available(p,self.t),'price':q.get('price'),'name':q.get('name',code),
                          'hold_days':sum(1 for d in official_days(self.t.year) or [] if str(d)>str(first)[:10] and str(d)<=self.t.date().isoformat())}
        return result
    def available(self,account,code):return self.positions().get(code,{}).get('avail',0)
    def view(self,account='ai',light=True):
        row=self._account();positions=list(self.positions().values());cash=row['cash_fen']/100
        if any(p['price'] is None for p in positions):raise ValueError('comparison valuation missing fresh quote')
        return {'cash':cash,'equity':cash+sum(p['qty']*p['price'] for p in positions),'positions':positions}
    def exposure(self,account):return self.view(account)
    def analyze(self,code):
        from ..analysis import analyze
        item=analyze(code)
        actual=item.get('quote') or {}; expected=self.raw_quotes.get(code) or {}
        if not expected or any(actual.get(k)!=expected.get(k) for k in ('price','time')):
            self.gap('same_batch_analysis_unavailable',code)
            raise ValueError('same-batch legacy analysis unavailable')
        return item
    def gap(self,reason,code=None):
        self.log(reason)
        payload={'book':self.book,'code':code,'reason':reason}
        identity=hashlib.sha256(json.dumps([self.book,iso(self.t),code,reason]).encode()).hexdigest()
        with db.tx() as c:
            c.execute('INSERT OR IGNORE INTO forecast_ledger_events VALUES(?,?,?,?)',
                      (identity,iso(self.t),'legacy_data_gap',db.dumps(payload)))
    def place(self,account,code,side,qty,**kwargs):
        if code in self.pending():raise ValueError('comparison order already pending')
        quote=self.raw_quotes.get(code)
        reason=kwargs.get('reason','legacy policy')
        identity='legacy:'+hashlib.sha256(json.dumps([iso(self.t),code,side,qty,reason],ensure_ascii=False).encode()).hexdigest()
        order=ledger.submit(self.book,code,side,qty,identity,quote,self.t,reason=reason,
                            expires_at=iso(min(self.t+timedelta(minutes=5),self.t.replace(hour=15,minute=0,second=0))))
        if order['status'] not in ('pending','filled'):raise ValueError(order['reason'])
        if side=='buy':
            self.order_terms[order['id']]={k:kwargs.get(k) for k in ('stop','target1','target2')}
        return {**order,'name':(quote or {}).get('name',code),'fill_price':None}
    def risk_tick(self):
        for code,p in self.positions().items():
            q=self.raw_quotes.get(code)
            if not q:
                self.gap('comparison_risk_quote_missing',code);continue
            qty,reason=sim.exit_decision(p,q['price'],p['avail'],self.settings['risk'],self.t.date().isoformat())
            if reason and code not in self.pending():
                try:self.place('ai',code,'sell',qty,reason=reason)
                except ValueError as exc:self.log(str(exc))
    def save(self):
        payload={'buffers':{c:list(b) for c,b in self.buffers.items()},'sources':self.sources,
                 'best':self.best,'cool':self.cool,'state':self.state,'initial':self.initial,
                 'order_terms':self.order_terms,'positions_snapshot':self.positions()}
        with db.tx() as c:c.execute('INSERT OR REPLACE INTO forecast_legacy_state VALUES(?,?)',(self.book,db.dumps(payload)))

def ready(book=BOOK):
    return bool(db.q1("SELECT 1 FROM sqlite_master WHERE name='forecast_books'") and
                db.q1('SELECT 1 FROM forecast_books WHERE id=?',(book,)))

def tick(quotes,asof=None,book=BOOK):
    t=stamp(asof) if asof else now()
    if db.writes_frozen() or not ready(book) or not t or not in_session(t,official_days(t.year)):
        return {'status':'waiting'}
    with _lock:
        ctx=Context(quotes,t,book=book)
        try:
            ctx.risk_tick()
            if ctx.settings.get('pick',{}).get('ai_mode','low')!='high':return {'status':'low_frequency'}
            return hf.tick(ctx)
        except (ValueError,RuntimeError) as exc:
            ctx.gap(str(exc));return {'status':'data_gap','reason':str(exc)}
        finally:ctx.save()

def on_picks(picks,session,book=None):
    if db.writes_frozen():return {'status':'waiting'}
    if book is None:
        if not db.q1("SELECT 1 FROM sqlite_master WHERE name='forecast_books'"):
            return {'status':'waiting'}
        books=[r['id'] for r in db.q("SELECT id FROM forecast_books WHERE id='legacy_control' OR id LIKE 'legacy_control@%'")]
        return {'books':{name:on_picks(picks,session,name) for name in books}}
    if not ready(book):return {'status':'waiting'}
    from . import runtime
    with _lock:
        with runtime._lock:quotes=dict(runtime._quotes)
        ctx=Context(quotes,now(),book=book)
        ctx.long=[dict(p) for p in picks]
        try:sim.auto_follow(picks,session,context=ctx)
        except (ValueError,RuntimeError) as exc:
            ctx.gap(str(exc));return {'status':'data_gap','reason':str(exc)}
        finally:ctx.save()
    return {'status':'evaluated','policy':'original_auto_follow'}
