"""Auditable feature families: measured values, missing evidence, never indicator votes."""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from statistics import mean, pstdev

from .. import indicators, models as math_models
from ..config import CN_TZ

VERSION = "comprehensive-features-2"
REGISTRY = (
    ("chan", "缠论结构链", "缠论", "结构引擎确认记录", True),
    ("trend", "均线/EMA/回归/高低点趋势", "趋势跟随与道氏趋势代理", "OHLC", True),
    ("momentum", "收益率/MACD/RSI/KDJ/CCI/威廉指标", "动量与反转", "OHLC", True),
    ("volume", "量比/OBV/CMF/MFI/量价相关", "量价与资金压力代理", "OHLCV", True),
    ("risk", "ATR/波动率/下行风险/回撤/VaR/CVaR", "风险与收益分布", "OHLC", True),
    ("math", "对数回归/Z分数/半衰期/Hurst/AR1/周期能量", "统计回归与均值回归", "OHLC", True),
    ("valuation", "PE/PB/流通市值/换手率", "相对估值", "带时点行情", False),
    ("fundamental", "ROE/负债率/现金转换/盈利率", "财务质量与价值研究", "当时可用公告", False),
    ("flow", "公开主力资金/资金强度", "资金流观察", "有来源与时间的公开资金数据", False),
    ("market", "指数收益/广度/市场风险状态", "市场状态与相对强弱", "同期指数和市场快照", False),
    ("sector", "板块收益/相对强度/扩散", "行业轮动", "同期行业快照", False),
    ("microstructure", "价差/委比/买卖盘深度", "市场微观结构代理", "有效五档快照", False),
    ("event", "公告与新闻事件", "事件驱动", "公开事件时间与可用时点", False),
    ("unimplemented", "波浪计数/江恩/完整DCF/宏观因果模型", "待验证理论目录", "未实现或证据不足", False),
)


def stamp(value):
    if not value:
        return None
    try:
        text = str(value)
        t = datetime.strptime(text, "%Y%m%d%H%M%S") if len(text) == 14 and text.isdecimal() else datetime.fromisoformat(text)
        return t.replace(tzinfo=CN_TZ) if t.tzinfo is None else t.astimezone(CN_TZ)
    except (ValueError, TypeError):
        return None


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _corr(a, b):
    if len(a) < 3 or len(a) != len(b) or not pstdev(a) or not pstdev(b):
        return None
    am, bm = mean(a), mean(b)
    return mean([(x-am)*(y-bm) for x, y in zip(a, b)]) / (pstdev(a)*pstdev(b))


def build(rows, *, asof, chan=None, quote=None, context=None, disclosure=None):
    """Compute only from bars visible at asof. Optional inputs require known_at/source.

    Missing families stay absent and receive explicit missing flags; zero is only an
    actually measured zero. Price scale is normalized for cross-stock comparison.
    """
    t = stamp(asof)
    if not t:
        raise ValueError("invalid feature asof")
    valid, excluded = [], 0
    for r in rows:
        dt, available = stamp(r.get("date")), stamp(r.get("available_at"))
        if dt and len(str(r.get("date"))) == 10:
            dt = dt.replace(hour=15)
        if (not dt or dt > t or not available or available > t or available < dt or r.get("complete") is False
                or any(not finite(r.get(k)) or r[k] <= 0 for k in ("open", "high", "low", "close"))
                or not finite(r.get("vol")) or r["vol"] < 0
                or r["low"] > min(r["open"], r["close"]) or r["high"] < max(r["open"], r["close"])):
            excluded += 1
            continue
        valid.append({**r,'date':dt.isoformat(),'available_at':available.isoformat()})
    valid.sort(key=lambda r: stamp(r["date"]))
    if len({r["date"] for r in valid}) != len(valid):
        raise ValueError("duplicate feature bars")
    out, reasons = {}, {}

    def put(k, v):
        if finite(v):
            out[k] = round(float(v), 10)

    if len(valid) >= 20:
        c, v = [r["close"] for r in valid], [r["vol"] for r in valid]
        p, ind = c[-1], indicators.pack(valid)
        returns = [math.log(b/a) for a, b in zip(c, c[1:])]
        for n in (5, 10, 20, 60):
            if len(c) >= n:
                put(f"trend_ma{n}_distance", p/mean(c[-n:])-1)
                put(f"trend_ema{n}_distance", p/indicators.ema(c, n)[-1]-1)
                put(f"trend_slope{n}", indicators.slope(c, n)/100)
            if len(c) > n:
                put(f"momentum_return{n}", p/c[-n-1]-1)
        for k in ("dif", "dea", "hist", "atr", "boll_up", "boll_dn"):
            x = ind[k][-1]
            if finite(x):
                put(("risk_" if k in ("atr", "boll_up", "boll_dn") else "momentum_")+k, x/p)
        for k in ("rsi6", "rsi14", "k", "d", "j"):
            x = ind[k][-1]
            if finite(x):
                put("momentum_"+k, x/100)
        h20, l20 = max(r["high"] for r in valid[-20:]), min(r["low"] for r in valid[-20:])
        put("trend_range_position20", (p-l20)/(h20-l20) if h20 > l20 else .5)
        put("trend_high_change10", max(r["high"] for r in valid[-10:])/max(r["high"] for r in valid[-20:-10])-1)
        put("trend_low_change10", min(r["low"] for r in valid[-10:])/min(r["low"] for r in valid[-20:-10])-1)
        put("momentum_williams14", (max(r["high"] for r in valid[-14:])-p)/max(1e-12, max(r["high"] for r in valid[-14:])-min(r["low"] for r in valid[-14:])))
        tp = [(r["high"]+r["low"]+r["close"])/3 for r in valid]
        tm = mean(tp[-20:]); dev = mean([abs(x-tm) for x in tp[-20:]])
        put("momentum_cci20", (tp[-1]-tm)/(.015*dev) if dev else 0)
        vs = sum(v[-20:])
        if vs:
            put("volume_ratio5_20", mean(v[-5:])/mean(v[-20:]))
            put("volume_obv20", sum((1 if c[i]>c[i-1] else -1 if c[i]<c[i-1] else 0)*v[i] for i in range(max(1,len(c)-20),len(c)))/vs)
            put("volume_cmf20", sum(((2*r["close"]-r["high"]-r["low"])/(r["high"]-r["low"]) if r["high"]>r["low"] else 0)*r["vol"] for r in valid[-20:])/vs)
            pos = sum(tp[i]*v[i] for i in range(max(1,len(c)-14),len(c)) if tp[i]>tp[i-1])
            neg = sum(tp[i]*v[i] for i in range(max(1,len(c)-14),len(c)) if tp[i]<tp[i-1])
            put("volume_mfi14", pos/(pos+neg) if pos+neg else .5)
        put("volume_return_correlation20", _corr(returns[-20:],v[-len(returns[-20:]):]))
        for n in (20,60):
            if len(returns) < n:
                continue
            rr=returns[-n:]; mu=mean(rr); sd=pstdev(rr); sr=sorted(rr)
            var=sr[max(0,int(.05*n)-1)]
            put(f"risk_vol{n}",sd); put(f"risk_downside{n}",math.sqrt(mean([min(0,x)**2 for x in rr])))
            put(f"risk_var05_{n}",var); put(f"risk_cvar05_{n}",mean([x for x in rr if x<=var]))
            if sd:
                put(f"risk_skew{n}",mean([((x-mu)/sd)**3 for x in rr]))
                put(f"risk_kurtosis{n}",mean([((x-mu)/sd)**4 for x in rr])-3)
        peak=c[0]; mdd=0
        for x in c:
            peak=max(peak,x); mdd=max(mdd,1-x/peak)
        put("risk_drawdown",mdd)
        ev=0
        for x in returns:
            ev=.94*ev+.06*x*x
        put("risk_ewma",math.sqrt(ev))
        math_out=math_models.compute(valid,ind)
        for k in ("z20","half_life","hurst"):
            put("math_"+k,math_out.get(k))
        reg=math_out.get("reg") or {}
        for k in ("r2","z","slope_ann"):
            put("math_reg_"+k,reg.get(k))
        for k in ("sd", "slope_day"):
            put("math_reg_"+k,reg.get(k))
        # These are observed distribution summaries, never a forecast or a vote.
        vol_metrics=math_out.get("vol") or {}
        for k in ("vol20_ann", "ewma_ann", "sharpe60", "mdd60"):
            put("risk_"+k,vol_metrics.get(k))
        squeeze=math_out.get("bbw") or {}
        for k in ("bbw", "pctile", "recent_min_pctile"):
            put("risk_boll_"+k,squeeze.get(k))
        if len(returns)>=21:
            previous, following=returns[-21:-1],returns[-20:]
            am,bm=mean(previous),mean(following)
            variance=sum((x-am)**2 for x in previous)
            if variance:
                put("math_ar1",sum((x-am)*(y-bm) for x,y in zip(previous,following))/variance)
        if len(c)>=60:
            y=[math.log(x) for x in c[-60:]]; ym=mean(y)
            slope=sum((i-29.5)*(x-ym) for i,x in enumerate(y))/sum((i-29.5)**2 for i in range(60))
            z=[x-ym-slope*(i-29.5) for i,x in enumerate(y)]; total=sum(x*x for x in z)
            energies=[sum(z[i]*math.cos(2*math.pi*k*i/60) for i in range(60))**2+sum(z[i]*math.sin(2*math.pi*k*i/60) for i in range(60))**2 for k in range(2,13)]
            if total>1e-15:
                best=max(range(len(energies)),key=energies.__getitem__)
                put("math_cycle_bars",60/(best+2));put("math_cycle_concentration",energies[best]/max(sum(energies),1e-12))
    else:
        reasons.update({k:"insufficient_completed_bars" for k in ("trend","momentum","volume","risk","math")})

    for structure in (chan or {}).values():
        if structure.get("confirmed") and (structure.get("quality") or {}).get("eligible"):
            tf=str(structure.get("timeframe","unknown"))
            for k,value in structure.get("features",{}).items():
                put(f"chan_{tf}_{k.removeprefix('chan_')}",value)
    if not any(k.startswith("chan_") for k in out):
        reasons["chan"]="no_confirmed_structure"
    q=quote or {}; qt=stamp(q.get("source_time") or q.get("time"))
    qknown=stamp(q.get("known_at") or q.get("received_at"))
    if qt and qknown and qt <= qknown <= t and 0 <= (t-qt).total_seconds() <= 12 and q.get("source"):
        for k in ("pe","pb","turnover","mcap","fmcap","vr"):
            put("valuation_"+k,q.get(k))
        bid=q.get("bid") or q.get("bids") or []; ask=q.get("ask") or q.get("asks") or []
        if bid and ask and isinstance(bid[0],(list,tuple)) and isinstance(ask[0],(list,tuple)):
            try:
                bp,ap=float(bid[0][0]),float(ask[0][0]); bv=sum(float(x[1]) for x in bid[:5]);av=sum(float(x[1]) for x in ask[:5])
                if bp>0 and ap>=bp:
                    put("microstructure_spread",(ap-bp)/((ap+bp)/2))
                    if all(math.isfinite(float(x[1])) and float(x[1]) >= 0 for x in bid[:5]+ask[:5]):
                        put("microstructure_imbalance",(bv-av)/(bv+av) if bv+av else None)
            except (TypeError,ValueError,IndexError):
                pass
    else:
        reasons.update(valuation="quote_missing_or_stale",microstructure="quote_missing_or_stale")
    for family in ("flow","market","sector","event"):
        ctx=(context or {}).get(family) or {}; kt=stamp(ctx.get("known_at")); st=stamp(ctx.get("source_time"))
        ttl=86400 if family=="event" else 180
        daily = family == "flow" and ctx.get("granularity") == "day" and ctx.get("source_precision") == "date"
        if daily:
            # Daily flow is a completed-period feature, not a 180-second live feed.
            from .calendar import official_days
            completed=[d for d in official_days(t.year) if d<t.date().isoformat()]
            fresh=bool(st and completed and st.date().isoformat()==max(completed))
        else:
            fresh=bool(st and 0 <= (t-st).total_seconds() <= ttl)
        if kt and st and st<=kt<=t and fresh and ctx.get("source"):
            for k,value in ctx.get("values",{}).items():
                put(family+"_"+k,value)
        else:
            reasons[family]=((context or {}).get('_coverage',{}).get(family) or {}).get('reason') or "missing_or_unavailable_at_prediction"
    d=disclosure or {}; dt=stamp(d.get("available_at"))
    if dt and dt<=t and d.get("source"):
        vals=d.get("values",{})
        for k in ("roe","debt_ratio"):
            put("fundamental_"+k,vals.get(k))
        profit,revenue,ocf=(vals.get(k) for k in ("profit_ttm","revenue_ttm","operating_cashflow_ttm"))
        if finite(profit) and finite(revenue) and revenue>0:
            put("fundamental_profit_margin",profit/revenue)
        if finite(profit) and finite(ocf) and profit>0:
            put("fundamental_cash_conversion",ocf/profit)
    else:
        reasons["fundamental"]="no_point_in_time_disclosure"
    coverage=[]
    for family,label,theory,needs,core in REGISTRY:
        count=sum(k.startswith(family+"_") for k in out)
        coverage.append({"id":family,"name":label,"theory":theory,"requires":needs,"core":core,
                         "count":count,"status":"computed" if count else "unavailable",
                         "reason":None if count else reasons.get(family,"not_implemented" if family=="unimplemented" else "missing_input"),
                         "source_coverage":((context or {}).get(family) or {}).get('coverage')})
        out["missing_"+family]=float(count==0)
    payload={"version":VERSION,"values":out,"coverage":coverage,"bars":len(valid),"excluded":excluded,
             "asof":t.isoformat(),"policy":"measured_features_not_equal_weight_votes"}
    payload["hash"]=hashlib.sha256(json.dumps(payload,sort_keys=True,allow_nan=False).encode()).hexdigest()
    return payload
