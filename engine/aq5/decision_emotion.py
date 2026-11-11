"""Bounded public theme/price/LHB evidence; identity-unknown seats are never hot money facts."""
from __future__ import annotations

import hashlib
import json
import math
import re
import statistics
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

from . import market
from .config import CN_TZ, DATA, now

VERSION = "emotion-public-v1"
ENDPOINT = "https://datacenter-web.eastmoney.com/api/data/v1/get"
BOARD_HOSTS = ("https://push2.eastmoney.com", "https://79.push2.eastmoney.com", "https://17.push2.eastmoney.com")
STYLE = re.compile("昨日|融资|融券|指数|沪股通|深股通|百元|低价|高价|风格|MSCI|标准普尔|富时罗素|预盈|预增|注册制")


def _at(value=None):
    t = now() if value is None else datetime.fromisoformat(str(value)) if not isinstance(value, datetime) else value
    if t.tzinfo is None:
        raise ValueError("asof must include timezone")
    return t.astimezone(CN_TZ)


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _get(url, *, sina_var=False):
    """Save byte-exact responses under configured AQ5_DATA, never under user home."""
    raw = market.http_get(url, "https://data.eastmoney.com/", timeout=12, retries=0)
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError("oversize_public_response")
    if sina_var:
        text = raw.decode("gbk")
        match = re.search(r"=\s*(\{.*\})", text, re.S)
        if not match:raise ValueError("invalid_sina_board_response")
        payload = json.loads(match.group(1))
    else:
        payload = json.loads(raw)
    observed = now().isoformat(timespec="seconds")
    digest = _sha(raw)
    root = Path(DATA) / "decision-public" / "emotion"
    root.mkdir(parents=True, exist_ok=True)
    path = root / (digest + ".json")
    if not path.exists():
        path.write_bytes(raw)
    receipt = {"provider": "新浪公开网页接口" if "sina.com.cn" in url else "腾讯公开网页接口" if "gtimg.cn" in url else "东方财富公开网页接口", "raw_sha256": digest, "retrieved_at": observed,
               "source_url": url, "raw_path": str(path), "rule_version": VERSION,
               "method": "公开响应解析与版本化规则推断；未经交易所逐项核验"}
    return payload, receipt


def _clist(params):
    failures = []
    for host in BOARD_HOSTS:
        url = host + "/api/qt/clist/get?" + urlencode({"pn": 1, "pz": 100, "po": 1, "np": 1,
              "ut": "bd1d9ddb04089700cf9c27f6f7426281", "fltt": 2, "invt": 2, **params})
        try:
            value, receipt = _get(url)
            data = value.get("data") or {}
            if value.get("rc") != 0 or not isinstance(data.get("diff"), list):
                raise ValueError("invalid_board_response")
            return data, receipt
        except (RuntimeError, ValueError, OSError) as exc:
            failures.append(type(exc).__name__)
    raise RuntimeError("board_sources_unavailable:" + ",".join(failures))


def _boards(max_pages=6):
    rows, receipts, total = [], [], 0
    for page in range(1, max_pages + 1):
        data, receipt = _clist({"pn": page, "fid": "f3", "fs": "m:90 t:3 f:!50",
             "fields": "f12,f14,f3,f104,f105,f124"})
        total = int(data.get("total") or 0)
        rows.extend(data["diff"]); receipts.append(receipt)
        if len(rows) >= total or len(data["diff"]) < 100:
            break
    unique = {r["f12"]: r for r in rows if re.fullmatch(r"BK\d+", str(r.get("f12")))}
    return list(unique.values()), receipts, total, len(unique) == total


def _members(board, max_pages=6):
    rows, receipts, total = [], [], 0
    for page in range(1, max_pages + 1):
        data, receipt = _clist({"pn": page, "fid": "f6", "fs": "b:" + board + " f:!50",
            "fields": "f12,f13,f14,f2,f3,f6,f8,f24,f124"})
        total = int(data.get("total") or 0)
        rows.extend(data["diff"]); receipts.append(receipt)
        if len(rows) >= total or len(data["diff"]) < 100:
            break
    unique = {str(r.get("f13")) + "." + str(r.get("f12")): r for r in rows}
    return list(unique.values()), receipts, total, len(unique) == total


def _history(code, at):
    if not code.startswith('BK'):
        url='https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?'+urlencode({'param':code+',day,,,80,qfq'})
        raw,receipt=_get(url)
        node=(raw.get('data') or {}).get(code) or {}
        bars=node.get('qfqday') or (node.get('day') if code=='sh000300' else None)
        if not isinstance(bars,list):raise ValueError('tencent_qfq_history_missing')
        klines=[','.join([str(p[0]),str(p[1]),str(p[2]),str(p[3]),str(p[4]),str(p[5]),'0']) for p in bars if len(p)>=6]
        raw={'rc':0,'data':{'klines':klines}}
    else:
        return _board_history(code,at)
    return _normalize_history(raw,receipt,at)


def _board_history(code, at):
    secid = "90." + code if code.startswith("BK") else ("1." if code.startswith("sh") else "0.") + code[2:]
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get?" + urlencode({"secid": secid,
        "fields1": "f1,f2,f3,f4,f5,f6", "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": 101, "fqt": 0 if code.startswith("BK") else 1,
        "beg": (at - timedelta(days=100)).strftime("%Y%m%d"), "end": at.strftime("%Y%m%d"), "lmt": 65})
    try:raw, receipt = _get(url)
    except (RuntimeError,ValueError,OSError):
        raw,receipt=_get(url.replace('https://push2his.eastmoney.com','https://91.push2his.eastmoney.com'))
    return _normalize_history(raw,receipt,at)


def _normalize_history(raw, receipt, at):
    if raw.get("rc") != 0 or not isinstance(raw.get("data"), dict):
        raise ValueError("history_unavailable")
    rows = []
    for item in raw["data"].get("klines", []):
        p = item.split(",")
        if len(p) < 7:
            raise ValueError("incomplete_history_fields")
        date = datetime.fromisoformat(p[0]).replace(hour=15, tzinfo=CN_TZ)
        if date <= at:
            row = {"date": p[0], "close": float(p[2]), "high": float(p[3]), "low": float(p[4]), "vol": float(p[5]), "amount": float(p[6])}
            if not all(_finite(row[k]) and row[k] >= 0 for k in ("close", "high", "low", "vol", "amount")) or row["close"] <= 0:
                raise ValueError("invalid_history_number")
            rows.append(row)
    if len({r["date"] for r in rows}) != len(rows) or rows != sorted(rows, key=lambda r: r["date"]):
        raise ValueError("history_dates_not_unique_ordered")
    if len(rows) < 21 or (at.date() - datetime.fromisoformat(rows[-1]["date"]).date()).days > 5:
        raise ValueError("insufficient_or_stale_history")
    return rows, receipt


def _record(code, kind, note, source_at, receipts, at, *, value=True, context=None):
    # Observed-time evidence is not backdated to the beginning of the trading day.
    source_at = _at(source_at)
    expiry = source_at.replace(hour=23, minute=59, second=59) + timedelta(days=3)
    if source_at > at or expiry <= at:
        return None
    combined = _sha(json.dumps([r["raw_sha256"] for r in receipts], separators=(",", ":")).encode())
    return {"code": code, "kind": kind, "value": value, "note": note, "source_url": receipts[0]["source_url"],
            "published_at": source_at.isoformat(timespec="seconds"),
            "available_at": max([at, *(_at(r["retrieved_at"]) for r in receipts)]).isoformat(timespec="seconds"),
            "expires_at": expiry.isoformat(timespec="seconds"),
            "provenance": {**receipts[0], "raw_sha256": combined, "raw_receipts": receipts,
                           "retrieved_at": max(r["retrieved_at"] for r in receipts), "rule_version": VERSION,
                           "context_id": context}}


def board_signal(board, rows):
    valid = _finite(board.get("f3")) and _finite(board.get("f104")) and _finite(board.get("f105"))
    count = board.get("f104", 0) + board.get("f105", 0) if valid else 0
    breadth = board["f104"] / count if count else None
    r5 = rows[-1]["close"] / rows[-6]["close"] - 1 if len(rows) >= 21 else None
    r20 = rows[-1]["close"] / rows[-21]["close"] - 1 if len(rows) >= 21 else None
    return {"eligible": bool(valid and count >= 5 and breadth >= .6 and board["f3"] >= 1 and r5 is not None and r5 > 0),
            "breadth": breadth, "return5": r5, "return20": r20}


def core_signal(member, members, rows, board):
    valid = [r for r in members if _finite(r.get("f24"))]
    rank = 1 + sum(r["f24"] > member["f24"] for r in valid) if _finite(member.get("f24")) else None
    r20 = rows[-1]["close"] / rows[-21]["close"] - 1 if len(rows) >= 21 else None
    # Persistent leadership: 60-day relative rank plus independently fetched 20-day performance.
    evaluated = bool(len(valid) >= 5 and len(valid) >= .8 * len(members) and rank and r20 is not None)
    eligible = bool(evaluated
                    and rank <= max(2, math.ceil(.2 * len(valid))) and member["f24"] > 0
                    and r20 is not None and r20 > max(.05, board.get("return20") or 0))
    close = rows[-1]["close"] if rows else 0
    peak = max(r["close"] for r in rows[-10:]) if rows else 0
    v3 = statistics.mean(r["vol"] for r in rows[-3:]) if len(rows) >= 21 else 0
    vbase = statistics.mean(r["vol"] for r in rows[-13:-3]) if len(rows) >= 21 else 0
    cooling = bool(eligible and peak and close <= peak * .97 and vbase and v3 / vbase <= .8
                   and close >= statistics.mean(r["close"] for r in rows[-20:]))
    return {"evaluated": evaluated, "cooling_evaluated": bool(evaluated and vbase > 0), "eligible": eligible, "return20": r20, "rank60": rank, "rank_denominator": len(valid),
            "cooling": cooling, "drawdown_from10close": close / peak - 1 if peak else None,
            "volume3_prior10": v3 / vbase if vbase else None}


def seat_proxy(rows, code, trade_date):
    groups = {}
    for row in rows:
        if row.get("SECURITY_CODE") != code[2:] or str(row.get("TRADE_DATE", ""))[:10] != trade_date:
            continue
        if not all(_finite(row.get(k)) and row[k] >= 0 for k in ("BUY", "SELL")):
            continue
        key = (str(row.get("CHANGE_TYPE")), str(row.get("TRADE_ID")))
        group=groups.setdefault(key, {}); seat_id=str(row.get("OPERATEDEPT_CODE"))
        if seat_id in ('0','None',''):
            # '机构专用' uses code 0 for several distinct anonymous disclosure rows.
            # Keep different amounts; only byte-equivalent numerical rows deduplicate.
            seat_id=('anonymous',str(row.get('OPERATEDEPT_NAME')),row['BUY'],row['SELL'])
        if seat_id in group and any(group[seat_id].get(k)!=row.get(k) for k in ('BUY','SELL','OPERATEDEPT_NAME')):
            raise ValueError('conflicting_duplicate_seat')
        group[seat_id] = row
    # Different abnormal-period lists may overlap: never sum all of them together.
    seats = list(max(groups.values(), key=lambda group: sum(x["BUY"] + x["SELL"] for x in group.values())).values()) if groups else []
    def category(name):
        if re.search("机构专用|股通专用|自营|交易单元", name):
            return "institution_or_connect"
        return "brokerage_seat_proxy" if re.search("营业部|分公司", name) else "unknown"
    detail = [{"name": r.get("OPERATEDEPT_NAME"), "seat_code": r.get("OPERATEDEPT_CODE"),
               "category": category(str(r.get("OPERATEDEPT_NAME", ""))), "buy": r["BUY"], "sell": r["SELL"],
               "net": r["BUY"] - r["SELL"]} for r in seats]
    ordinary = [r for r in detail if r["category"] == "brokerage_seat_proxy"]
    total_buy = sum(r["buy"] for r in detail)
    return {"code": code, "trade_date": trade_date, "seats": detail,
            "non_institution_buy": sum(r["buy"] for r in ordinary),
            "non_institution_net": sum(r["net"] for r in ordinary),
            "non_institution_buy_share": sum(r["buy"] for r in ordinary) / total_buy if total_buy else None,
            "hot_money_identity": "UNKNOWN", "eligible_hot_money_fact": False,
            "note": "营业部席位是非机构参与代理，不能识别实际投资者；不据此断言知名游资。多异常期榜单取一组，未累加。代码0的匿名机构保留不同金额披露行，仅完全相同行去重，不能据此确认机构数量。"}


def _focus_fallback(codes, at):
    """Associated boards remain inspectable when ranking/member endpoint is down.

    This establishes a relative-strength mainline candidate only, never core identity.
    """
    records, receipts, assessments, failures = [], [], [], []
    benchmark, bench_receipt = _history("sh000300", at)
    receipts.append(bench_receipt)
    br5 = benchmark[-1]["close"] / benchmark[-6]["close"] - 1
    br20 = benchmark[-1]["close"] / benchmark[-21]["close"] - 1
    for code in codes:
        try:
            raw, affiliation = _get("https://emweb.securities.eastmoney.com/PC_HSF10/CoreConception/PageAjax?code=" + code.upper())
            receipts.append(affiliation)
            names = [r for r in raw.get("ssbk", []) if r.get("SECURITY_CODE") == code[2:] and r.get("IS_PRECISE") == "1"
                     and str(r.get("BOARD_CODE", "")).isdigit() and not STYLE.search(str(r.get("BOARD_NAME", "")))]
            for row in names[:3]:
                board_code = "BK" + str(row["BOARD_CODE"]).zfill(4)
                try:
                    history, hr = _history(board_code, at); receipts.append(hr)
                    quote, qr = _get("https://push2.eastmoney.com/api/qt/stock/get?" + urlencode({"secid":"90."+board_code,"fields":"f57,f58,f170,f86","fltt":2}));receipts.append(qr)
                    q = quote.get("data") or {}
                    if q.get("f57") != board_code or not _finite(q.get("f170")) or not _finite(q.get("f86")):
                        raise ValueError("board_quote_incomplete")
                    source_at = datetime.fromtimestamp(q["f86"], CN_TZ)
                    r5 = history[-1]["close"] / history[-6]["close"] - 1
                    r20 = history[-1]["close"] / history[-21]["close"] - 1
                    valid = (source_at <= at and (at-source_at).total_seconds() <= 5*86400
                             and history[-1]["date"] == benchmark[-1]["date"]
                             and q["f170"] >= 1 and r5 > max(0, br5) and r20 > max(0, br20))
                    assessments.append({"code":code,"board":row["BOARD_NAME"],"mainline_relative_candidate":valid,
                                        "return5":r5,"return20":r20,"benchmark5":br5,"benchmark20":br20,
                                        "core":"UNKNOWN","breadth":"UNKNOWN","member_rank":"UNKNOWN"})
                    if valid:
                        rec = _record(code,"mainline",f"关联板块{row['BOARD_NAME']}多日相对强度主线候选：当日{q['f170']}%，5日{r5:.2%}/沪深300 {br5:.2%}，20日{r20:.2%}/基准{br20:.2%}。成分排名和广度接口未齐，不能据此确认核心。",source_at,[affiliation,hr,qr,bench_receipt],at,context=board_code)
                        if rec:records.append(rec)
                except (RuntimeError,ValueError,KeyError,TypeError,OSError) as exc:
                    failures.append({"stage":"focus_board","code":code,"board":board_code,"reason":str(exc)[:180]})
        except (RuntimeError,ValueError,KeyError,TypeError,OSError) as exc:
            failures.append({"stage":"focus_affiliation","code":code,"reason":str(exc)[:180]})
    return records,receipts,assessments,failures


def _sina_fallback(codes, at, max_boards, max_history):
    """Complete small industry constituents + their actual completed daily histories.

    Large groups exceeding the explicit history budget remain UNKNOWN; never sample-rank a core.
    """
    root="https://vip.stock.finance.sina.com.cn"
    raw, receipt = _get(root+"/q/view/newSinaHy.php",sina_var=True)
    boards=[]
    for value in raw.values():
        p=value.split(',')
        if len(p)>=13 and p[0].startswith('new_') and not STYLE.search(p[1]):
            boards.append({'node':p[0],'name':p[1],'count':int(p[2]),'pct':float(p[5])})
    chosen=sorted(boards,key=lambda b:-b['pct'])[:max_boards]
    records,receipts,assessments,failures=[],[receipt],[],[]; fetched=0;seen=set()
    for board in chosen:
        try:
            if board['count']>max_history-fetched:
                failures.append({'stage':'sina_history_budget','board':board['name'],'members':board['count']});continue
            url=root+"/quotes_service/api/json_v2.php/Market_Center.getHQNodeData?"+urlencode({'page':1,'num':100,'sort':'symbol','asc':1,'node':board['node'],'symbol':'','_s_r_a':'init'})
            members,mr=_get(url);receipts.append(mr)
            if not isinstance(members,list):raise ValueError('sina_members_invalid')
            bycode={r.get('symbol'):r for r in members if re.fullmatch(r'(sh|sz)\d{6}',str(r.get('symbol','')))}
            if len(bycode)!=board['count']:raise ValueError('sina_member_count_mismatch')
            if not codes.intersection(bycode):continue
            histories={};hr=[]
            for code in bycode:
                rows,sr=_history(code,at);fetched+=1;histories[code]=rows;hr.append(sr);receipts.append(sr)
            last_dates={h[-1]['date'] for h in histories.values()}
            if len(last_dates)!=1 or any(len(h)<61 for h in histories.values()):raise ValueError('sina_histories_not_complete_aligned')
            r1=[h[-1]['close']/h[-2]['close']-1 for h in histories.values()]
            r5=statistics.median(h[-1]['close']/h[-6]['close']-1 for h in histories.values())
            r20=statistics.median(h[-1]['close']/h[-21]['close']-1 for h in histories.values())
            breadth=sum(v>0 for v in r1)/len(r1)
            mainline=len(r1)>=5 and statistics.median(r1)>=.01 and r5>0 and breadth>=.6
            comparison=[{'f24':(h[-1]['close']/h[-61]['close']-1)*100,'code':c} for c,h in histories.items()]
            source_at=datetime.fromisoformat(next(iter(last_dates))).replace(hour=15,tzinfo=CN_TZ)
            seen.update(codes.intersection(bycode))
            for member in comparison:
                code=member['code']
                if code not in codes:continue
                signal=core_signal(member,comparison,histories[code],{'return20':r20})
                assessments.append({'code':code,'board':board['name'],'source':'sina_members+eastmoney_daily','mainline':mainline,'members':len(bycode),'member_complete':True,**signal})
                rec=_record(code,'mainline',f"新浪行业{board['name']}完整{len(bycode)}家成分的已完成日线：当日涨幅中位{statistics.median(r1):.2%}，5日中位{r5:.2%}，上涨占比{breadth:.1%}。{'满足' if mainline else '不满足'}行业多日强势规则；非官方认定。",source_at,[receipt,mr,*hr],at,value=mainline,context=board["node"])
                if rec:records.append(rec)
                if signal['evaluated']:
                    rec=_record(code,'core',f"完整{len(bycode)}家行业对比：60日涨幅排名{signal['rank60']}/{signal['rank_denominator']}，20日{signal['return20']:.2%}，行业中位{r20:.2%}。{'满足' if signal['eligible'] else '不满足'}多日核心规则，不以单日涨幅判断。",source_at,[mr,*hr],at,value=signal['eligible'],context=board['node'])
                    if rec:records.append(rec)
                    if signal['cooling_evaluated']:
                        rec=_record(code,'cooling',f"完整多日资料：较10日高点{signal['drawdown_from10close']:.2%}、3日/前10日均量{signal['volume3_prior10']:.2f}；{'满足' if signal['cooling'] else '不满足'}核心缩量回调并保持MA20条件。",source_at,[mr,*hr],at,value=signal['cooling'],context=board['node'])
                        if rec:records.append(rec)
        except (RuntimeError,ValueError,KeyError,TypeError,OSError) as exc:
            failures.append({'stage':'sina_board','board':board['name'],'reason':str(exc)[:180]})
    return records,receipts,assessments,failures,{'boards':len(boards),'examined':len(chosen),'histories':fetched,'covered_codes':len(seen)}


def select_context(records):
    """Select one coherent board per stock; never splice facts across boards."""
    groups={}
    for record in records:
        context=record.get('provenance',{}).get('context_id') or 'unscoped'
        groups.setdefault(record['code'],{}).setdefault(context,{})[record['kind']]=record
    chosen,alternatives=[],[]
    for code,contexts in sorted(groups.items()):
        def rank(item):
            key,claims=item
            return (claims.get('mainline',{}).get('value') is not True,
                    not all(k in claims for k in ('mainline','core','cooling')),
                    claims.get('core',{}).get('value') is not True,key)
        winner,claims=min(contexts.items(),key=rank)
        chosen.extend(claims.values())
        alternatives.append({'code':code,'selected_context':winner,'contexts':[
            {'context':key,'values':{k:r['value'] for k,r in data.items()}} for key,data in sorted(contexts.items())]})
    return chosen,alternatives


def collect(snapshot, asof=None, *, max_boards=5, max_history=30, max_seats=12, focus_codes=None):
    at = _at(asof)
    if at > now() + timedelta(seconds=5):
        raise ValueError("future_asof")
    codes = {r["code"] for r in snapshot if market.is_main_board(str(r.get("code", ""))) and "ST" not in str(r.get("name", "")).upper()}
    records, failures, assessments, proxies, receipts = [], [], [], [], []
    coverage = {"input_count": len(snapshot), "mainboard_count": len(codes), "board_total": 0,
                "board_received": 0, "board_list_complete": False, "boards_examined": 0, "history_count": 0,
                "membership_codes": 0, "lhb_candidates": 0, "lhb_details": 0, "hot_money_identified": 0,
                "limits": {"max_boards": max_boards, "max_history": max_history, "max_seats": max_seats}}
    known_members = set()
    try:
        boards, br, total, complete = _boards(); receipts += br
        coverage.update(board_total=total, board_received=len(boards), board_list_complete=complete)
        chosen = sorted((b for b in boards if not STYLE.search(str(b.get("f14", ""))) and _finite(b.get("f3"))), key=lambda b: -b["f3"])[:max_boards]
        for board in chosen:
            try:
                source_at = datetime.fromtimestamp(board["f124"], CN_TZ)
                if source_at > at or (at - source_at).total_seconds() > 5 * 86400:
                    raise ValueError("board_timestamp_stale_or_future")
                hist, hr = _history(board["f12"], at); receipts.append(hr)
                signal = board_signal(board, hist)
                coverage["boards_examined"] += 1
                if not signal["eligible"]:
                    continue
                members, mr, member_total, member_complete = _members(board["f12"]); receipts += mr
                matched = [("sh" if r.get("f13") == 1 else "sz") + str(r.get("f12")) for r in members]
                known_members.update(c for c in matched if c in codes)
                for member, code in zip(members, matched):
                    if code not in codes:
                        continue
                    rec = _record(code, "mainline", f"规则主线候选：{board['f14']}；当天{board['f3']}%，板块5日{signal['return5']:.2%}，上涨占比{signal['breadth']:.1%}。仅已检查的强势概念；非官方主线认定。", source_at, [br[0], hr, *mr], at,context=board["f12"])
                    if rec: records.append(rec)
                    if coverage["history_count"] >= max_history or not member_complete:
                        continue
                    stock, sr = _history(code, at); receipts.append(sr); coverage["history_count"] += 1
                    core = core_signal(member, members, stock, signal)
                    assessments.append({"code": code, "board": board["f14"], "member_total": member_total,
                                        "member_complete": member_complete, **core})
                    if core["evaluated"]:
                        rec = _record(code, "core", f"完整成分规则对比：{board['f14']}成分60日涨幅排名{core['rank60']}/{core['rank_denominator']}；独立日线20日{core['return20']:.2%}。{'满足' if core['eligible'] else '不满足'}多日核心规则，非官方身份。", source_at, [hr, sr, *mr], at,value=core['eligible'],context=board['f12'])
                        if rec: records.append(rec)
                        if core['cooling_evaluated']:
                            rec = _record(code, "cooling", f"核心回调规则：较10日收盘高点{core['drawdown_from10close']:.2%}；3日均量/此前10日均量{core['volume3_prior10']:.2f}；{'满足' if core['cooling'] else '不满足'}核心缩量回调/MA20条件。不是低关注度的直接测量。", source_at, [sr], at,value=core['cooling'],context=board['f12'])
                            if rec: records.append(rec)
            except (RuntimeError, ValueError, KeyError, TypeError, OSError) as exc:
                failures.append({"stage": "board", "code": board.get("f12"), "reason": str(exc)[:180]})
    except (RuntimeError, ValueError, KeyError, TypeError, OSError) as exc:
        failures.append({"stage": "board_list", "reason": str(exc)[:180]})
    coverage["membership_codes"] = len(known_members)
    if not coverage["board_received"]:
        try:
            sr,ss,sa,sf,sc=_sina_fallback(codes,at,max_boards,max_history)
            records+=sr;receipts+=ss;assessments+=sa;failures+=sf;coverage['sina_fallback']=sc
        except (RuntimeError,ValueError,KeyError,TypeError,OSError) as exc:
            failures.append({'stage':'sina_fallback','reason':str(exc)[:180]})
    focuses = [c for c in (focus_codes or []) if c in codes]
    if not coverage["board_received"] or focuses:
        selected_focus = focuses or [r["code"] for r in sorted(snapshot,key=lambda r:-(r.get("pct") if _finite(r.get("pct")) else -999)) if r.get("code") in codes][:max_boards]
        coverage["focus_codes"] = selected_focus
        try:
            fr,fs,fa,ff = _focus_fallback(selected_focus,at)
            records += fr;receipts += fs;assessments += fa;failures += ff
        except (RuntimeError,ValueError,KeyError,TypeError,OSError) as exc:
            failures.append({"stage":"focus_fallback","reason":str(exc)[:180]})
    try:
        focused_lhb = focus_codes is not None
        lhb_codes = set(focuses) if focused_lhb else codes
        if focused_lhb and not lhb_codes:
            raise ValueError("lhb_no_eligible_focus_codes")
        params = {"reportName": "RPT_DAILYBILLBOARD_DETAILSNEW", "columns": "ALL", "pageNumber": 1, "pageSize": 500,
                  "sortTypes": "-1", "sortColumns": "TRADE_DATE", "source": "WEB", "client": "WEB",
                  "filter": f"(TRADE_DATE>='{(at-timedelta(days=10)).date()}')(TRADE_DATE<='{at.date()}')"}
        listing = []
        coverage["lhb_list_complete"] = True
        # Exact per-stock queries avoid spending the single-stock budget on the market list.
        # Still guard locally: a provider may ignore a supplied filter.
        for focus in sorted(lhb_codes) if focused_lhb else [None]:
            query = dict(params)
            if focus:
                query["filter"] += f'(SECURITY_CODE="{focus[2:]}")'
            page, pages = 1, 1
            while page <= pages:
                query["pageNumber"] = page
                value, lr = _get(ENDPOINT + "?" + urlencode(query)); receipts.append(lr)
                if value.get("success") is not True or not isinstance(value.get("result"), dict):
                    raise ValueError("lhb_listing_unavailable")
                result = value["result"]
                reported_pages = int(result.get("pages", 1))
                if reported_pages < 1 or focused_lhb and reported_pages > 20:
                    raise ValueError("lhb_focus_pagination_invalid_or_excessive")
                if page > 1 and reported_pages != pages:
                    raise ValueError("lhb_focus_pagination_changed")
                if not isinstance(result.get("data"), list):
                    raise ValueError("lhb_listing_rows_invalid")
                listing.extend(row for row in result["data"] if not focus or row.get("SECURITY_CODE") == focus[2:])
                if not focused_lhb:
                    coverage["lhb_list_complete"] = reported_pages == 1
                    break
                pages = reported_pages
                page += 1
        selected = {}
        for row in listing:
            code = ("sh" if str(row.get("SECURITY_CODE", "")).startswith("6") else "sz") + str(row.get("SECURITY_CODE", ""))
            day = str(row.get("TRADE_DATE", ""))[:10]
            available = datetime.fromisoformat(day).replace(hour=15, tzinfo=CN_TZ)
            # Intraday publication time is not supplied. Actual visibility is retrieval time,
            # never inferred as available at the start of the trade date.
            if code in lhb_codes and available <= at and (at.date()-available.date()).days <= 5:
                selected[code] = max(selected.get(code, day), day) if focused_lhb else selected.get(code, day)
        coverage["lhb_candidates"] = len(selected)
        for code, day in list(selected.items())[:len(lhb_codes) if focused_lhb else max_seats]:
            try:
                seats, sr = [], []
                for side in ("BUY", "SELL"):
                    p = {"reportName": "RPT_BILLBOARD_DAILYDETAILS" + side, "columns": "ALL", "pageNumber": 1,
                         "pageSize": 500, "sortTypes": "-1", "sortColumns": side, "source": "WEB", "client": "WEB",
                         "filter": f'(TRADE_DATE=\'{day}\')(SECURITY_CODE="{code[2:]}")'}
                    raw, receipt = _get(ENDPOINT + "?" + urlencode(p)); receipts.append(receipt); sr.append(receipt)
                    if raw.get("success") is not True or not isinstance(raw.get("result"), dict):
                        raise ValueError("lhb_seat_details_unavailable")
                    seats += raw["result"].get("data", [])
                proxy = seat_proxy(seats, code, day); proxy["provenance"] = sr
                proxy["available_at"] = max(r["retrieved_at"] for r in sr)
                proxy["published_at"] = None
                proxies.append(proxy)
                coverage["lhb_details"] += 1
            except (RuntimeError, ValueError, KeyError, TypeError, OSError) as exc:
                failures.append({"stage": "lhb_details", "code": code, "reason": str(exc)[:180]})
    except (RuntimeError, ValueError, KeyError, TypeError, OSError) as exc:
        coverage["lhb_list_complete"] = False
        failures.append({"stage": "lhb_list", "reason": str(exc)[:180]})
    chosen,alternatives=select_context(records)
    return {"version": VERSION, "asof": at.isoformat(), "records": chosen, "coverage": coverage,
            "selected_contexts":alternatives,
            "failures": failures, "assessments": assessments, "proxy_metrics": proxies, "sources": receipts,
            "full_market_screen": False, "note": "部分强势概念扫描；主线/核心/冷却是可解释规则推断。龙虎榜仅加分参考，未识别游资身份保持UNKNOWN。"}
