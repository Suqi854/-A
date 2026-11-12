"""Bounded public theme/price/LHB evidence; identity-unknown seats are never hot money facts."""
from __future__ import annotations

import hashlib
import gzip
import json
import math
import os
import re
import statistics
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
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


def _history(code, at, *, fetch=None, minimum_bars=21, max_stale_days=5):
    fetch = fetch or _get
    if not code.startswith('BK'):
        url='https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?'+urlencode({'param':code+',day,,,80,qfq'})
        raw,receipt=fetch(url)
        node=(raw.get('data') or {}).get(code) or {}
        bars=node.get('qfqday') or (node.get('day') if code=='sh000300' else None)
        if not isinstance(bars,list):raise ValueError('tencent_qfq_history_missing')
        klines=[','.join([str(p[0]),str(p[1]),str(p[2]),str(p[3]),str(p[4]),str(p[5]),'0']) for p in bars if len(p)>=6]
        raw={'rc':0,'data':{'klines':klines}}
    else:
        return _board_history(code,at,fetch=fetch)
    return _normalize_history(raw,receipt,at,minimum_bars=minimum_bars,max_stale_days=max_stale_days)


def _board_history(code, at, *, fetch=None):
    fetch = fetch or _get
    secid = "90." + code if code.startswith("BK") else ("1." if code.startswith("sh") else "0.") + code[2:]
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get?" + urlencode({"secid": secid,
        "fields1": "f1,f2,f3,f4,f5,f6", "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": 101, "fqt": 0 if code.startswith("BK") else 1,
        "beg": (at - timedelta(days=100)).strftime("%Y%m%d"), "end": at.strftime("%Y%m%d"), "lmt": 65})
    try:raw, receipt = fetch(url)
    except (RuntimeError,ValueError,OSError):
        raw,receipt=fetch(url.replace('https://push2his.eastmoney.com','https://91.push2his.eastmoney.com'))
    return _normalize_history(raw,receipt,at)


def _normalize_history(raw, receipt, at, *, minimum_bars=21, max_stale_days=5):
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
    if len(rows) < minimum_bars or rows and max_stale_days is not None and (at.date() - datetime.fromisoformat(rows[-1]["date"]).date()).days > max_stale_days:
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


FULL_VERSION = "emotion-full-mainboard-v2"
_SINA_ROOT = "https://vip.stock.finance.sina.com.cn"


class _BudgetEnd(Exception):
    pass


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _save_json(path, value, *, compressed=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = _json_bytes(value)
    tmp = path.with_name(path.name + f".{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_bytes(gzip.compress(raw) if compressed else raw)
    tmp.replace(path)


class _FullRun:
    """One frozen universe/data cutoff, resumable task and byte-source checkpoints."""
    def __init__(self, snapshot, asof, run_id, max_workers, budget_seconds, progress=None):
        self.at = _at(asof)
        if self.at > now() + timedelta(seconds=5):
            raise ValueError("future_asof")
        self.deadline = time.monotonic() + max(1, min(float(budget_seconds), 7200))
        self.workers = max(1, min(int(max_workers), 4))
        self.progress, self.last_progress = progress, 0.0
        self.rows, self.invalid = {}, []
        for index, row in enumerate(snapshot):
            code = market.norm_code(str(row.get("code", "")))
            if not re.fullmatch(r"(?:sh|sz)\d{6}", code) or not market.is_main_board(code):
                self.invalid.append({"index": index, "code": str(row.get("code", "")), "reason": "outside_mainboard_or_invalid"})
            else:
                self.rows.setdefault(code, dict(row, code=code))
        if not self.rows:
            raise ValueError("empty_mainboard_universe")
        scope_hash = _sha(_json_bytes(sorted(self.rows)))
        run_id = run_id or f"{self.at.date()}-{scope_hash[:12]}"
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", run_id):
            raise ValueError("invalid_emotion_run_id")
        self.id = run_id
        self.root = Path(DATA) / "decision-public" / "emotion" / "full" / run_id
        manifest_path = self.root / "manifest.json"
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text("utf-8"))
            if manifest.get("scope_hash") != scope_hash or manifest.get("version") != FULL_VERSION:
                raise ValueError("emotion_resume_scope_or_version_mismatch")
            if asof is not None and _at(manifest["asof"]) != self.at:
                raise ValueError("emotion_resume_cutoff_mismatch")
            self.at = _at(manifest["asof"])
            self.rows = manifest["universe"]
            self.invalid = manifest["invalid_input_rows"]
        else:
            manifest = {"version": FULL_VERSION, "asof": self.at.isoformat(), "scope_hash": scope_hash,
                        "universe": self.rows, "invalid_input_rows": self.invalid, "input_rows": len(snapshot),
                        "scope_note": "固定沪深主板目录；ST/停牌保留分母。只比较完整源成分的主板投影。"}
            _save_json(manifest_path, manifest)
        self.manifest = manifest

    def check(self):
        if time.monotonic() >= self.deadline:
            raise _BudgetEnd()

    def read(self, key):
        path = self.root / "tasks" / (_sha(key.encode()) + ".json.gz")
        if not path.exists():
            return None
        try:
            saved = json.loads(gzip.decompress(path.read_bytes()))
            if saved["key"] != key or saved["checksum"] != _sha(_json_bytes(saved["result"])):
                return None
            def verify(value):
                if isinstance(value, dict):
                    if "raw_path" in value and "raw_sha256" in value:
                        candidate = Path(DATA) / "decision-public" / "emotion" / (value["raw_sha256"] + ".json")
                        if not candidate.exists() or _sha(candidate.read_bytes()) != value["raw_sha256"]:
                            raise ValueError("cached_raw_unavailable")
                        value["raw_path"] = str(candidate)
                    else:
                        for child in value.values():verify(child)
                elif isinstance(value, list):
                    for child in value:verify(child)
            verify(saved["result"])
            return saved["result"]
        except (ValueError, KeyError, OSError):
            return None

    def work(self, key, fn):
        previous = self.read(key)
        if previous and (previous["status"] == "ok" or previous["attempts"] >= 2):
            return previous
        self.check()
        attempts = (previous or {}).get("attempts", 0) + 1
        try:
            value = fn()
            result = {"status": "ok", "attempts": attempts, "value": value, "received_at": now().isoformat()}
        except _BudgetEnd:
            raise
        except (RuntimeError, ValueError, KeyError, TypeError, OSError) as exc:
            result = {"status": "error", "attempts": attempts, "reason": str(exc)[:300], "error_type": type(exc).__name__}
        path = self.root / "tasks" / (_sha(key.encode()) + ".json.gz")
        _save_json(path, {"key": key, "checksum": _sha(_json_bytes(result)), "result": result}, compressed=True)
        return result

    def fetch(self, url, *, sina_var=False, phase=""):
        result = self.work("http:" + phase + ":" + url, lambda: _get(url, sina_var=sina_var))
        if result["status"] != "ok":
            raise RuntimeError(result["reason"])
        value, receipt = result["value"]
        raw_path = Path(receipt["raw_path"])
        if not raw_path.exists() or _sha(raw_path.read_bytes()) != receipt["raw_sha256"]:
            raise ValueError("emotion_cached_raw_missing_or_corrupt")
        return value, receipt

    def jobs(self, jobs, stage="sources"):
        """Never enqueue the whole universe: at most workers live requests past cutoff."""
        result, todo = {}, []
        for key, fn in jobs.items():
            saved = self.read(key)
            if saved and (saved["status"] == "ok" or saved["attempts"] >= 2):
                result[key] = saved
            else:
                todo.append((key, fn))
        def report(force=False):
            current = time.monotonic()
            if self.progress and (force or current - self.last_progress >= 10):
                self.progress({"stage": stage, "completed": sum(v is not None for v in result.values()),
                               "requested": len(jobs), "updated_at": now().isoformat()})
                self.last_progress = current
        report(True)
        iterator = iter(todo)
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            pending = {}
            def submit():
                if time.monotonic() >= self.deadline:
                    return False
                item = next(iterator, None)
                if item is None:
                    return False
                key, fn = item
                pending[pool.submit(self.work, key, fn)] = key
                return True
            for _ in range(self.workers):
                if not submit():break
            while pending:
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    key = pending.pop(future)
                    try:result[key] = future.result()
                    except _BudgetEnd:result[key] = self.read(key)
                    report()
                    submit()
        report(True)
        return {key: result.get(key) or self.read(key) for key in jobs}


def _full_catalog(kind, fetch):
    url = _SINA_ROOT + "/q/view/newSinaHy.php" if kind == "industry" else "https://money.finance.sina.com.cn/q/view/newFLJK.php?param=class"
    raw, receipt = fetch(url, sina_var=True)
    if not isinstance(raw, dict) or not raw:
        raise ValueError("empty_sina_catalog")
    boards = []
    for value in raw.values():
        p = str(value).split(",")
        if len(p) < 13 or not re.fullmatch(r"(?:new|gn)_[A-Za-z0-9_]+", p[0]) or not p[2].isdigit():
            raise ValueError("unparsed_sina_catalog_entry")
        boards.append({"id": "sina:" + kind + ":" + p[0], "node": p[0], "name": p[1], "kind": kind,
                       "catalog_display_count": int(p[2]), "scope": "新浪行业代理" if kind == "industry" else "新浪概念分类"})
    if len({b["id"] for b in boards}) != len(boards):
        raise ValueError("duplicate_sina_catalog_node")
    return {"boards": boards, "receipts": [receipt]}


def _full_members(board, fetch):
    count_url = _SINA_ROOT + "/quotes_service/api/json_v2.php/Market_Center.getHQNodeStockCount?" + urlencode({"node": board["node"]})
    count, before = fetch(count_url)
    if not re.fullmatch(r"\d+", str(count)) or not 0 <= int(count) <= 10000:
        raise ValueError("invalid_sina_constituent_count")
    count = int(count)
    rows, receipts = [], [before]
    for page in range(1, math.ceil(count / 100) + 1):
        url = _SINA_ROOT + "/quotes_service/api/json_v2.php/Market_Center.getHQNodeData?" + urlencode({"page": page, "num": 100, "sort": "symbol", "asc": 1, "node": board["node"], "symbol": "", "_s_r_a": "init"})
        part, receipt = fetch(url)
        if not isinstance(part, list) or not part or len(part) > 100:
            raise ValueError("sina_constituent_page_incomplete")
        rows.extend(part); receipts.append(receipt)
    after_count, after = fetch(count_url, phase="after-pages")
    receipts.append(after)
    symbols = [r.get("symbol") for r in rows]
    if (str(after_count) != str(count) or len(set(symbols)) != count
            or any(not re.fullmatch(r"(?:(?:sh|sz|bj)\d{6}|hk\d{5})", str(c)) for c in symbols)):
        raise ValueError("sina_constituent_count_drift_or_duplicates")
    mainboard = sorted(c for c in symbols if market.is_main_board(c))
    return {"board": board, "source_total": count, "source_symbols": symbols, "mainboard_members": mainboard,
            "non_mainboard_count": count - len(mainboard), "source_membership_complete": True,
            "catalog_count_is_display_only": count != board["catalog_display_count"], "receipts": receipts}


def _full_lhb_pages(params, fetch):
    rows, receipts, expected_pages, expected_count, seen = [], [], None, None, set()
    page = 1
    while expected_pages is None or page <= expected_pages:
        raw, receipt = fetch(ENDPOINT + "?" + urlencode({**params, "pageNumber": page, "pageSize": 500}))
        receipts.append(receipt)
        result = raw.get("result")
        if raw.get("success") is not True or not isinstance(result, dict) or not isinstance(result.get("data"), list):
            raise ValueError("lhb_all_pages_unavailable")
        pages, count = int(result.get("pages", -1)), int(result.get("count", -1))
        if pages < 0 or count < 0 or pages != math.ceil(count / 500):
            raise ValueError("lhb_pagination_metadata_invalid")
        if expected_pages is not None and (pages != expected_pages or count != expected_count):
            raise ValueError("lhb_pagination_metadata_changed")
        expected_pages, expected_count = pages, count
        for row in result["data"]:
            identity = _sha(_json_bytes(row))
            if identity in seen:raise ValueError("lhb_duplicate_page_row")
            seen.add(identity); rows.append(row)
        page += 1
    if len(rows) != expected_count:
        raise ValueError("lhb_all_pages_row_count_mismatch")
    return {"rows": rows, "receipts": receipts, "complete": True, "row_count": expected_count}


def _full_lhb_listing(at, fetch):
    end = at.date() if at.hour >= 15 else (at-timedelta(days=1)).date()
    start = end-timedelta(days=10)
    result = _full_lhb_pages({"reportName": "RPT_DAILYBILLBOARD_DETAILSNEW", "columns": "ALL", "sortTypes": "-1",
        "sortColumns": "TRADE_DATE", "source": "WEB", "client": "WEB",
        "filter": f"(TRADE_DATE>='{start}')(TRADE_DATE<='{end}')"}, fetch)
    for row in result["rows"]:
        if not re.fullmatch(r"\d{6}", str(row.get("SECURITY_CODE", ""))):
            raise ValueError("lhb_listing_security_code_invalid")
        try:day = datetime.fromisoformat(str(row.get("TRADE_DATE", ""))[:10]).date()
        except ValueError:raise ValueError("lhb_listing_date_invalid") from None
        if not start <= day <= end:
            raise ValueError("lhb_listing_date_outside_frozen_window")
    result["window"] = {"start": start.isoformat(), "end": end.isoformat()}
    return result


def _full_lhb_seats(code, day, fetch):
    rows, receipts = [], []
    for side in ("BUY", "SELL"):
        result = _full_lhb_pages({"reportName": "RPT_BILLBOARD_DAILYDETAILS" + side, "columns": "ALL",
            "sortTypes": "-1", "sortColumns": side, "source": "WEB", "client": "WEB",
            "filter": f'(TRADE_DATE=\'{day}\')(SECURITY_CODE="{code[2:]}")'}, fetch)
        if any(r.get("SECURITY_CODE") != code[2:] or str(r.get("TRADE_DATE", ""))[:10] != day for r in result["rows"]):
            raise ValueError("lhb_detail_filter_not_respected")
        rows += result["rows"]; receipts += result["receipts"]
    result = seat_proxy(rows, code, day)
    if not result["seats"]:
        raise ValueError("lhb_listed_but_no_usable_seats")
    result.update(provenance=receipts, available_at=max(r["retrieved_at"] for r in receipts), published_at=None)
    return result


def _full_context(membership, histories, anchor_day, at):
    """Source membership is complete; ranking denominator is its frozen-mainboard projection."""
    board, members = membership["board"], membership["target_members"]
    proof = {"context_id": board["id"], "name": board["name"], "kind": board["kind"], "scope": board["scope"],
             "source_total": membership["source_total"], "source_membership_complete": True,
             "target_members": members, "target_member_count": len(members),
             "outside_universe_mainboard": membership["outside_universe_mainboard"],
             "non_mainboard_count": membership["non_mainboard_count"],
             "membership_refs": [r["raw_sha256"] for r in membership["receipts"]],
             "data_day": anchor_day, "history_refs": {}, "missing_history": [], "insufficient_60d": []}
    eligible_history = {}
    for code in members:
        result = histories.get("history:" + code)
        if not result or result["status"] != "ok":
            proof["missing_history"].append(code); continue
        rows, receipt = result["value"]
        proof["history_refs"][code] = receipt["raw_sha256"]
        if not rows or rows[-1]["date"] != anchor_day or len(rows) < 6:
            proof["missing_history"].append(code); continue
        eligible_history[code] = (rows, receipt)
        if len(rows) < 61:proof["insufficient_60d"].append(code)
    proof["mainline_input_complete"] = bool(anchor_day and len(members) >= 5 and not proof["missing_history"])
    proof["core_input_complete"] = proof["mainline_input_complete"] and not proof["insufficient_60d"]
    proof["proof_sha256"] = _sha(_json_bytes(proof))
    if not proof["mainline_input_complete"]:
        return [], proof
    histories_only = [rows for rows, _ in eligible_history.values()]
    r1 = [rows[-1]["close"] / rows[-2]["close"] - 1 for rows in histories_only]
    r5 = statistics.median(rows[-1]["close"] / rows[-6]["close"] - 1 for rows in histories_only)
    breadth = sum(x > 0 for x in r1) / len(r1)
    mainline = statistics.median(r1) >= .01 and r5 > 0 and breadth >= .6
    r20 = statistics.median(rows[-1]["close"] / rows[-21]["close"] - 1 for rows in histories_only) if proof["core_input_complete"] else None
    comparison = [{"code": code, "f24": (rows[-1]["close"] / rows[-61]["close"] - 1) * 100}
                  for code, (rows, _) in eligible_history.items()] if proof["core_input_complete"] else []
    records = []
    source_at = datetime.fromisoformat(anchor_day).replace(hour=15, tzinfo=CN_TZ)
    for code, (rows, receipt) in eligible_history.items():
        common = membership["receipts"][:2] + [receipt]
        message = (f"{board['scope']} {board['name']}：完整源成分{membership['source_total']}，固定主板投影{len(members)}；"
                   f"完成日{anchor_day}涨幅中位{statistics.median(r1):.2%}、5日中位{r5:.2%}、上涨比{breadth:.1%}。"
                   f"{'满足' if mainline else '不满足'}主线规则；不是官方主线认定。")
        record = _record(code, "mainline", message, source_at, common, at, value=mainline, context=board["id"])
        candidates = [record] if record else []
        if proof["core_input_complete"]:
            member = next(r for r in comparison if r["code"] == code)
            signal = core_signal(member, comparison, rows, {"return20": r20})
            message = (f"{board['name']}完整主板投影{len(members)}：60日排名{signal['rank60']}/{signal['rank_denominator']}，"
                       f"本股20日{signal['return20']:.2%}/成分中位{r20:.2%}；{'满足' if signal['eligible'] else '不满足'}核心规则。")
            record = _record(code, "core", message, source_at, common, at, value=signal["eligible"], context=board["id"])
            if record:candidates.append(record)
            if signal["cooling_evaluated"]:
                message = (f"{board['name']}同一主板核心规则；较10日收盘高点{signal['drawdown_from10close']:.2%}，"
                           f"3日/前10日均量{signal['volume3_prior10']:.2f}；{'满足' if signal['cooling'] else '不满足'}核心缩量回调/MA20条件。")
                record = _record(code, "cooling", message, source_at, common, at, value=signal["cooling"], context=board["id"])
                if record:candidates.append(record)
        for record in candidates:
            record["provenance"].update(rule_version=FULL_VERSION, context_evidence_sha256=proof["proof_sha256"],
                                        method="完整新浪分类成分的固定沪深主板投影；共享同一已完成日线时点；规则推断")
        records += candidates
    return records, proof


def collect_full(snapshot, asof=None, *, run_id=None, max_workers=3, budget_seconds=900, progress=None):
    """Account for every supplied mainboard code; source gaps never count as qualified coverage.

    Call again with the same run_id (and no new asof) to resume. Persist the whole
    AQ5_DATA/decision-public/emotion tree between jobs. Only one job owns a run_id.
    """
    run = _FullRun(snapshot, asof, run_id, max_workers, budget_seconds, progress)
    at, codes = run.at, set(run.rows)
    initial = run.jobs({"catalog:industry": lambda: _full_catalog("industry", run.fetch),
                        "catalog:concept": lambda: _full_catalog("concept", run.fetch),
                        "lhb:list": lambda: _full_lhb_listing(at, run.fetch)})
    sources, all_tasks, boards = {}, dict(initial), []
    def remember(receipts):
        for receipt in receipts:sources[receipt["raw_sha256"]] = receipt
    catalog_status = {}
    for kind in ("industry", "concept"):
        result = initial.get("catalog:" + kind)
        catalog_status[kind] = result["status"] if result else "pending"
        if result and result["status"] == "ok":
            boards += result["value"]["boards"]; remember(result["value"]["receipts"])
    members = run.jobs({"members:" + b["id"]: (lambda board=b: _full_members(board, run.fetch)) for b in boards}, stage="memberships")
    all_tasks.update(members)
    membership_by_code = {code: {"industry": [], "concept": []} for code in codes}
    qualified_members, source_extras = {}, set()
    for board in boards:
        result = members.get("members:" + board["id"])
        if not result or result["status"] != "ok":continue
        value = dict(result["value"])
        value["target_members"] = sorted(codes.intersection(value["mainboard_members"]))
        value["outside_universe_mainboard"] = sorted(set(value["mainboard_members"]) - codes)
        source_extras.update(value["outside_universe_mainboard"])
        qualified_members[board["id"]] = value; remember(value["receipts"])
        for code in value["target_members"]:membership_by_code[code][board["kind"]].append(board["id"])
    # Every target gets a history check, including new listings, ST, halted and unmapped names.
    histories = run.jobs({"history:" + code: (lambda c=code: _history(c, at, fetch=run.fetch, minimum_bars=0, max_stale_days=None))
                          for code in ["sh000300", *sorted(codes)]}, stage="daily_histories")
    all_tasks.update(histories)
    anchor = histories.get("history:sh000300")
    anchor_day = anchor["value"][0][-1]["date"] if anchor and anchor["status"] == "ok" and anchor["value"][0] else None
    anchor_age = (at.date()-datetime.fromisoformat(anchor_day).date()).days if anchor_day else None
    anchor_qualified = anchor_age is not None and 0 <= anchor_age <= 5
    for result in histories.values():
        if result and result["status"] == "ok":remember([result["value"][1]])
    context_proofs, records = {}, []
    for key, membership in qualified_members.items():
        candidates, proof = _full_context(membership, histories, anchor_day if anchor_qualified else None, at)
        context_proofs[key] = proof; records += candidates
    selected, alternatives = select_context(records)
    listing = initial.get("lhb:list")
    lhb_complete = bool(listing and listing["status"] == "ok")
    pairs, lhb_rows = set(), []
    if lhb_complete:
        remember(listing["value"]["receipts"])
        for row in listing["value"]["rows"]:
            code = ("sh" if str(row.get("SECURITY_CODE", "")).startswith("6") else "sz") + str(row.get("SECURITY_CODE", ""))
            day = str(row.get("TRADE_DATE", ""))[:10]
            try:visible = datetime.fromisoformat(day).replace(hour=15, tzinfo=CN_TZ) <= at
            except ValueError:visible = False
            if code in codes and visible:
                pairs.add((code, day))
                lhb_rows.append({"code": code, "trade_date": day, "trade_id": row.get("TRADE_ID"), "reason": row.get("EXPLANATION")})
    seats = run.jobs({"lhb:" + code + ":" + day: (lambda c=code, d=day: _full_lhb_seats(c, d, run.fetch)) for code, day in sorted(pairs)}, stage="lhb_details")
    all_tasks.update(seats)
    latest_dates = {}
    for code, day in pairs:latest_dates[code] = max(latest_dates.get(code, day), day)
    proxies, lhb_events = [], {}
    for (code, day) in sorted(pairs):
        result = seats.get("lhb:" + code + ":" + day)
        key = code + ":" + day
        lhb_events[key] = {"code": code, "trade_date": day, "status": result["status"] if result else "pending"}
        if result and result["status"] == "ok":
            proxy = result["value"]; remember(proxy["provenance"])
            lhb_events[key].update(seat_rows=len(proxy["seats"]), raw_refs=[r["raw_sha256"] for r in proxy["provenance"]])
            if day == latest_dates[code]:proxies.append(proxy)
        elif result:lhb_events[key]["reason"] = result["reason"]
    chosen_by_code = {}
    for record in selected:chosen_by_code.setdefault(record["code"], {})[record["kind"]] = record["value"]
    source_kind_complete = {kind: catalog_status[kind] == "ok" and all(
        members.get("members:" + b["id"]) and members["members:" + b["id"]]["status"] == "ok"
        for b in boards if b["kind"] == kind) for kind in ("industry", "concept")}
    outcomes = {}
    for code, row in sorted(run.rows.items()):
        h = histories.get("history:" + code)
        history_state, bars, data_day = "pending", None, None
        if h and h["status"] == "error":history_state = "fetch_or_parse_failed"
        elif h and h["status"] == "ok":
            rows = h["value"][0]; bars = len(rows); data_day = rows[-1]["date"] if rows else None
            history_state = ("insufficient_history" if bars < 61 else "benchmark_stale_or_unavailable" if not anchor_qualified
                             else "data_day_mismatch" if data_day != anchor_day else "qualified")
        mapping = membership_by_code[code]
        claim = chosen_by_code.get(code, {})
        events = [lhb_events[code + ":" + day]["status"] for c, day in pairs if c == code]
        lhb_state = ("not_listed_in_complete_window" if lhb_complete and not events else
                     "listed_details_complete" if events and all(x == "ok" for x in events) else
                     "listed_details_pending" if "pending" in events else
                     "listed_details_failed" if events else "listing_pending_or_failed")
        outcomes[code] = {"name": row.get("name"), "membership_covered": bool(mapping["industry"] or mapping["concept"]),
            "industry_contexts": mapping["industry"], "concept_contexts": mapping["concept"],
            "industry_mapping": "mapped" if mapping["industry"] else "unmapped" if source_kind_complete["industry"] else "source_incomplete",
            "concept_mapping": "mapped" if mapping["concept"] else "unmapped" if source_kind_complete["concept"] else "source_incomplete",
            "official_industry": {k: row.get(k) for k in ("industry", "industry_code", "industry_scheme", "industry_source", "directory_date")},
            "is_st_name_marker": row.get("is_st_name_marker", row.get("st_name_marker", "ST" in str(row.get("name", "")).upper())),
            "suspended": row.get("suspended"), "history": history_state, "history_bars": bars, "data_day": data_day,
            "mainline": claim.get("mainline", "UNKNOWN"), "core": claim.get("core", "UNKNOWN"), "cooling": claim.get("cooling", "UNKNOWN"),
            "lhb": lhb_state, "lhb_event_count": len(events), "hot_money_identity": "UNKNOWN"}
        if h and h["status"] == "error":outcomes[code]["history_reason"] = h["reason"]
    pending = [key for key, result in all_tasks.items() if result is None]
    failures = [{"task": key, "reason": result["reason"], "attempts": result["attempts"]}
                for key, result in all_tasks.items() if result and result["status"] == "error"]
    retryable = [f["task"] for f in failures if f["attempts"] < 2]
    unresolved = [code for code, o in outcomes.items() if o["history"] != "qualified" or not o["membership_covered"]
                  or any(o[k] == "UNKNOWN" for k in ("mainline", "core", "cooling"))
                  or o["lhb"] not in ("listed_details_complete", "not_listed_in_complete_window")]
    status = "incomplete" if pending or retryable else "complete_with_gaps" if failures or unresolved or run.invalid or not all(source_kind_complete.values()) else "complete"
    result = {"version": FULL_VERSION, "run_id": run.id, "asof": at.isoformat(), "data_day": anchor_day,
        "completed_at": now().isoformat(), "status": status, "records": selected, "proxy_metrics": proxies,
        "coverage": {"scope": "all_supplied_mainboard_codes", "input_rows": run.manifest["input_rows"], "universe_count": len(codes),
            "accounted_count": len(outcomes), "membership_covered": sum(o["membership_covered"] for o in outcomes.values()),
            "history_checked": sum(o["history"] != "pending" for o in outcomes.values()),
            "history_qualified": sum(o["history"] == "qualified" for o in outcomes.values()),
            "evidence_evaluated_count": len(chosen_by_code), "source_boards": len(boards), "source_memberships_complete": len(qualified_members),
            "source_kind_complete": source_kind_complete, "catalog_status": catalog_status,
            "lhb_list_complete": lhb_complete, "lhb_list_rows": listing["value"]["row_count"] if lhb_complete else None,
            "lhb_window": listing["value"].get("window") if lhb_complete else None,
            "lhb_pairs": len(pairs), "lhb_details_complete": sum(x["status"] == "ok" for x in lhb_events.values()),
            "unresolved_count": len(unresolved), "data_complete": status == "complete",
            "anchor_age_calendar_days": anchor_age, "anchor_qualified": anchor_qualified},
        "code_outcomes": outcomes, "unmapped_codes": [c for c, o in outcomes.items() if not o["membership_covered"]],
        "unresolved_codes": unresolved, "source_mainboard_outside_universe": sorted(source_extras),
        "invalid_input_rows": run.invalid, "contexts": context_proofs, "selected_contexts": alternatives,
        "lhb_listing": lhb_rows, "lhb_events": lhb_events, "sources": list(sources.values()), "failures": failures,
        "pending_tasks": pending, "retryable_tasks": retryable,
        "resume": {"run_id": run.id, "checkpoint": str(run.root), "same_asof_and_universe_required": True},
        "full_market_screen": False, "all_mainboard_requested": True,
        "note": "全部输入主板代码逐项留状态，ST/停牌不删分母；全部可用新浪行业/概念分类，不代表穷尽市场题材。仅使用同一完成日多日资料；缺口UNKNOWN，不把遍历完成当数据完整。"}
    _save_json(run.root / "result.json.gz", result, compressed=True)
    return result
