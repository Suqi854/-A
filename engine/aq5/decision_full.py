"""Independent resumable all-mainboard build; never starts the trading engine."""
from __future__ import annotations

import gzip
import hashlib
import hmac
import json
import math
import os
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

VERSION="full-mainboard-1"


def _require():
    if os.environ.get("AQ5_FULL_RESEARCH")!="1" or not os.environ.get("AQ5_DATA"):
        raise RuntimeError("isolated_full_research_required")
    if os.environ.get("AQ5_RESEARCH_BRANCH","research-cache")!="research-cache":
        raise RuntimeError("research_branch_mismatch")


def relay(action, body=None):
    from . import cloud
    password=cloud._key();base=cloud._cfg()["relay"].rstrip("/")
    if not password or not base:return {"ok":False,"reason":"relay_unconfigured"}
    token=hmac.new(password.encode(),b'aq5-live-publish-v1',hashlib.sha256).hexdigest()
    headers={"User-Agent":"aq5-full-research","x-aq5-live-key":token}
    data=None if body is None else json.dumps(body,separators=(",",":")).encode()
    if data is not None:headers["Content-Type"]="application/json"
    request=urllib.request.Request(base+"/__aq5/research/"+action,data=data,headers=headers)
    with urllib.request.urlopen(request,timeout=20) as response:return json.load(response)


def _save(path, value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    raw=json.dumps(value,ensure_ascii=False,separators=(",",":"),allow_nan=False).encode()
    temp=path.with_name(path.name+".tmp");temp.write_bytes(gzip.compress(raw) if path.suffix==".gz" else raw);temp.replace(path)


def _read(path):
    path=Path(path);raw=path.read_bytes()
    return json.loads(gzip.decompress(raw) if path.suffix==".gz" else raw)


def empty_summary(status=None):
    from .config import now
    build=(status or {}).get("build") or {}
    note="全沪深主板研究正在准备；首次全量采集可能需数十分钟，可稍后重新进入。"
    if build.get("phase") in ("error","partial"):note="全量研究尚有缺口，正在保留断点；未知数据不会补成合格。"
    return {"kind":"research_preview","version":VERSION,"not_new_selection":True,"asof":None,
        "generated_at":now().isoformat(),"quote_time_status":"unknown","scope":{"all_mainboard_requested":True,"data_complete":False},
        "routes":{r:{"label":label,"candidates":[],"watch":[],"covered":0,"universe_count":build.get("total") or 0,
                      "candidate_count":0,"note":note} for r,label in (("value","中短线价值 · 业绩催化"),("emotion","超短线情绪 · 主线核心"))},
        "full_research":status or {"build":{"phase":"queued"}}}


def latest_screen(*,start_if_needed=True):
    """Fast path: load compact evidence; start/resume the separate build if needed."""
    from . import decision_snapshot
    from .config import now, CN_TZ
    try:
        cached=decision_snapshot.read_summary()
    except (OSError,ValueError,KeyError,RuntimeError):
        cached={"status":"unavailable"}
    summary=cached.get("summary")
    fresh=False
    if summary:
        try:
            age=(now()-datetime.fromisoformat(summary["generated_at"]).astimezone(CN_TZ)).total_seconds()
            fresh=0<=age<6*3600 and summary.get("scope",{}).get("scan_complete") is True
        except (ValueError,TypeError,KeyError):pass
    try:
        status=relay("status") if fresh or not start_if_needed else relay("start",{})
    except (OSError,ValueError):
        status={"ok":False,"build":{"phase":"unavailable"},"reason":"研究构建通道暂不可用"}
    if not summary:return empty_summary(status)
    summary["full_research"]=status
    summary["snapshot_commit"]=cached.get("commit")
    # Per-code ledger stays in the encrypted audit snapshot; the request queue
    # carries only the compact candidate view within its 512 KiB result limit.
    summary.pop("coverage_manifest",None)
    if not fresh:
        updating=(status.get("build") or {}).get("phase") in ("queued","running","checkpoint")
        for group in summary["routes"].values():
            group["note"]+=(" 正在更新全量快照；" if updating else " 全量快照待更新；")+"当前仍展示原生成时点，不算新信号。"
    return summary


def make_summary(universe, rows, growth, emotion):
    from . import decision_evidence as e, decision_collect
    from .config import now
    codes={r["code"] for r in universe["rows"]};total=len(codes)
    if not codes or total!=len(universe["rows"]):raise ValueError("official_universe_invalid")
    received={};duplicates=set();extras=set()
    for row in rows:
        code=row.get("code")
        if code not in codes:extras.add(str(code));continue
        if code in received:duplicates.add(code)
        else:received[code]=row
    missing_rows=codes-set(received)
    canonical=[]
    for official in universe["rows"]:
        code=official["code"];row={**official,**received.get(code,{}),"code":code,"name":official.get("name",code)}
        if code in missing_rows or code in duplicates or not e.finite(row.get("price")) or row["price"]<=0:
            row["quote_status"]="missing_or_ambiguous"
        if official.get("is_st_name_marker"):row["research_exclusion"]="ST名称风险标记"
        elif row.get("suspended") is True:row["research_exclusion"]="已明确停牌"
        elif row.get("quote_status")!="available":row["research_exclusion"]="无合格报价"
        canonical.append(row)
    rows=canonical
    g=growth.get("status_by_code",{});m=emotion.get("code_outcomes",{})
    active={};ingestion={}
    for label,report in (("growth",growth),("emotion",emotion)):
        receipt=decision_collect._ingest(report);ingestion[label]=receipt
        for code,ids in receipt["active_ids"].items():active.setdefault(code,[]).extend(ids)
    e.record_observation(codes,active)
    routes=e.screen(rows,now())
    ledger={};gfetched=gqualified=gnonapp=gmissing=missing_catalyst=eevaluated=eunknown=excluded=0
    value_unknown=set();quote_missing=set();lhb_missing=set()
    for row in rows:
        code=row["code"];financial=g.get(code,{}).get("financial",{});catalyst=g.get(code,{}).get("catalyst",{})
        fstatus=financial.get("status","missing");estate=m.get(code,{})
        fetched=financial.get("fetched") is True;attempted=financial.get("attempted") is True
        if (fstatus not in ("qualified","nonapplicable") or not fetched or not attempted
                or fstatus=="qualified" and type(financial.get("value")) is not bool
                or fstatus=="nonapplicable" and not financial.get("reason")):fstatus="missing"
        catalyst_required=fstatus=="qualified" and financial.get("value") is True
        catalyst_gap=catalyst_required and not (catalyst.get("status")=="qualified" and
                       catalyst.get("attempted") is True and type(catalyst.get("value")) is bool)
        qualifies=(all(type(estate.get(k)) is bool for k in ("mainline","core","cooling"))
                   and estate.get("history")=="qualified" and estate.get("membership_covered") is True)
        gfetched+=fetched;gqualified+=fstatus=="qualified";gnonapp+=fstatus=="nonapplicable";gmissing+=fstatus=="missing"
        missing_catalyst+=catalyst_gap;eevaluated+=qualifies;eunknown+=not qualifies;excluded+=bool(row.get("research_exclusion"))
        if fstatus=="missing" or catalyst_gap:value_unknown.add(code)
        if row.get("quote_status")!="available":quote_missing.add(code)
        if estate.get("lhb") not in ("listed_details_complete","not_listed_in_complete_window"):lhb_missing.add(code)
        ledger[code]={"name":row["name"],"financial":fstatus,"financial_reason":financial.get("reason"),
            "catalyst":catalyst.get("status","not_evaluated"),"catalyst_required":catalyst_required,"history":estate.get("history","pending"),
            "membership":bool(estate.get("membership_covered")),"emotion_evaluated":qualifies,
            "lhb":estate.get("lhb","pending"),"quote":row.get("quote_status"),"excluded":row.get("research_exclusion")}
    accounted=set(g)==codes and set(m)==codes and not missing_rows and not duplicates and not extras
    scan_complete=bool(universe.get("verified") is True and accounted and
                       growth.get("coverage",{}).get("all_requested_attempted") is True and
                       all(g[c].get("financial",{}).get("attempted") is True for c in codes) and
                       emotion.get("status") in ("complete","complete_with_gaps") and
                       not emotion.get("pending_tasks") and not emotion.get("retryable_tasks"))
    rejected=any(v["rejected"] for v in ingestion.values())
    data_complete=(scan_complete and not value_unknown and not eunknown and not quote_missing and not lhb_missing
                   and not rejected and not growth.get("failures") and not emotion.get("failures")
                   and emotion.get("coverage",{}).get("data_complete") is True)
    routes["value"].update(covered=gfetched,universe_count=total,note=f"官方全沪深主板{total}股逐项核对；财务取得{gfetched}，可计算{gqualified}，规则非适用{gnonapp}，财务缺口{gmissing}，必要催化缺口{missing_catalyst}。ST/停牌/无合格报价不入候选，但不删覆盖分母。")
    routes["emotion"].update(covered=eevaluated,universe_count=total,note=f"官方全沪深主板{total}股逐项核对；形成主线/核心/冷却证据{eevaluated}，未知或不足{eunknown}。基于全部可用新浪分类，非穷尽市场题材；龙虎榜只加分。完成日：{emotion.get('data_day') or '未知'}。")
    for route in routes.values():
        route["note"]+=" 全部遍历不代表数据全部合格。" if not data_complete else " 来源完整性检查通过，候选仍需个股缠论确认。"
    stamp=now().isoformat()
    return {"kind":"research_preview","version":VERSION,"not_new_selection":True,"asof":emotion.get("data_day"),
        "generated_at":stamp,"quote_time_status":"known" if all(r.get("quote_status")=="available" for r in rows) else "partial",
        "scope":{"all_mainboard_requested":True,"universe_verified":universe.get("verified"),"universe_count":total,
                 "sse":universe["coverage"]["sse"],"szse":universe["coverage"]["szse"],"st_in_denominator":universe["coverage"]["st_name_markers"],
                 "accounted_count":len(ledger),"scan_complete":scan_complete,"data_complete":bool(data_complete),
                 "quote_missing_count":len(quote_missing),"lhb_missing_count":len(lhb_missing),
                 "input_row_issues":{"missing":sorted(missing_rows),"duplicates":sorted(duplicates),"extra":sorted(extras)},
                 "selection":"官方全部沪深主板目录；不按成交额、市值或报价是否存在缩小采集范围"},
        "routes":routes,"coverage_manifest":ledger,
        "collection":{"version":VERSION,"scope":"all_official_mainboard","sources":{
            "universe":{k:universe.get(k) for k in ("verified","directory_date","universe_hash","coverage")},
            "growth":{"coverage":growth.get("coverage"),"failures":growth.get("failures",[])[:30]},
            "emotion":{"coverage":emotion.get("coverage"),"status":emotion.get("status"),"failures":emotion.get("failures",[])[:30]}}},
        "coverage":{"value":{"completed":sum(g.get(c,{}).get("financial",{}).get("attempted") is True for c in codes),"total":total,"eligible":gqualified,"unknown":len(value_unknown),"excluded":gnonapp},
                    "emotion":{"completed":len(m),"total":total,"eligible":eevaluated,"unknown":eunknown,"excluded":excluded}},
        "ingestion_errors":{k:v["rejected"][:20] for k,v in ingestion.items()}}


def build(*,budget_seconds=4500,persist=True):
    _require()
    from . import db, market, decision, decision_snapshot as store, decision_universe, decision_growth, decision_emotion
    from .config import DATA, now
    db.init();root=DATA/"decision-full";root.mkdir(parents=True,exist_ok=True)
    build_id=os.environ.get("AQ5_RESEARCH_BUILD_ID","")
    mutex=threading.RLock();stage_state={};last_emit=[0.0]
    def notify(source,receipt):
        with mutex:
            stage_state[source]=receipt
            if time.monotonic()-last_emit[0]<25:return
            last_emit[0]=time.monotonic()
        body={"build_id":build_id,"phase":"running","stage":(source+":"+str(receipt.get("stage","")))[:40],
              "completed":int(receipt.get("completed",0)),"total":int(receipt.get("requested",receipt.get("total",0))),
              "run_id":os.environ.get("GITHUB_RUN_ID","0")}
        if body["completed"]>body["total"]:body["total"]=body["completed"]
        print(json.dumps({"stage":body["stage"],"completed":body["completed"],"total":body["total"]}),flush=True)
        if build_id:
            try:relay("progress",body)
            except (OSError,ValueError):pass
    if persist:
        try:print(json.dumps(store.restore()),flush=True)
        except (OSError,ValueError,RuntimeError) as exc:print("Research checkpoint unavailable: "+type(exc).__name__,flush=True)
    universe=decision_universe.collect(storage_dir=DATA/"decision-universe")
    _save(root/"universe-report.json",universe)
    if not universe.get("verified"):
        print(json.dumps({"stage":"official_universe","failures":universe.get("failures",[])},ensure_ascii=False),flush=True)
        raise RuntimeError("official_mainboard_directory_unavailable")
    codes=[r["code"] for r in universe["rows"]]
    if len(codes)!=len(set(codes)) or not codes:raise ValueError("official_universe_invalid")
    state_path=root/"state.json";previous=_read(state_path) if state_path.exists() else {}
    scope_hash=hashlib.sha256(json.dumps(sorted(codes)).encode()).hexdigest()
    collector_versions={"growth":decision_growth.VERSION,"emotion":decision_emotion.FULL_VERSION}
    version_hash=hashlib.sha256(json.dumps(collector_versions,sort_keys=True).encode()).hexdigest()[:8]
    reuse=(previous.get("scope_hash")==scope_hash and previous.get("started_at","")[:10]==now().date().isoformat()
           and previous.get("collector_versions")==collector_versions)
    started_at=previous["started_at"] if reuse else now().isoformat()
    run_id=previous["run_id"] if reuse else now().strftime("%Y%m%d")+"-"+scope_hash[:12]+"-"+version_hash
    _save(state_path,{"scope_hash":scope_hash,"started_at":started_at,"run_id":run_id,"universe":universe,"collector_versions":collector_versions})
    rows=[];quotes=market.quotes(codes);quote_at=now()
    for official in universe["rows"]:
        quote=quotes.get(official["code"],{});row={**quote,**official}
        if type(quote.get("suspended")) is bool:row["suspended"]=quote["suspended"]
        try:
            qt=datetime.fromisoformat(decision.quote_time(quote.get("source_time") or quote.get("time")))
            price=quote.get("price")
            available=type(price) in (int,float) and math.isfinite(price) and price>0 and 0<=(quote_at-qt).total_seconds()<5*86400
        except (ValueError,TypeError):available=False
        row["quote_status"]="available" if available else "missing_or_stale"
        row["research_exclusion"]="ST名称风险标记" if official.get("is_st_name_marker") else "已明确停牌" if quote.get("suspended") is True else "无合格报价" if not available else None
        rows.append(row)
    stop=threading.Event()
    def checkpoint_loop():
        while not stop.wait(300):
            try:
                with mutex:progress={"stages":dict(stage_state),"run_id":run_id,"universe_count":len(codes)}
                result=store.publish(checkpoint=store.archive(),progress=progress)
                print("Research checkpoint "+result["commit"],flush=True)
            except (OSError,ValueError,RuntimeError) as exc:print("Checkpoint retained locally: "+type(exc).__name__,flush=True)
    checkpoint=threading.Thread(target=checkpoint_loop,daemon=True) if persist else None
    if checkpoint:checkpoint.start()
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            f=executor.submit(decision_growth.collect,codes,progress=lambda r:notify("value",r))
            s=executor.submit(decision_emotion.collect_full,rows,asof=None if reuse else started_at,run_id=run_id,
                              budget_seconds=budget_seconds,max_workers=3,progress=lambda r:notify("emotion",r))
            growth,emotion=f.result(),s.result()
        _save(root/"growth.json.gz",growth);_save(root/"emotion.json.gz",emotion)
        summary=make_summary(universe,rows,growth,emotion);_save(root/"summary.json",summary)
    finally:
        stop.set()
        if checkpoint:checkpoint.join()
    phase="complete" if summary["scope"]["data_complete"] else "partial"
    publication=store.publish(summary=summary,checkpoint=store.archive(),progress={"phase":phase,"coverage":summary["coverage"],"run_id":run_id}) if persist else {}
    if build_id:
        relay("progress",{"build_id":build_id,"phase":phase,"stage":"完成检查", "completed":len(codes),"total":len(codes),
                          "coverage":summary["coverage"],**({"snapshot_commit":publication["commit"]} if publication else {})})
    print(json.dumps({"phase":phase,"scope":summary["scope"],"coverage":summary["coverage"],"publication":publication},ensure_ascii=False),flush=True)
    return summary


def main():
    _require()
    try:build()
    except Exception as exc:
        try:relay("progress",{"build_id":os.environ.get("AQ5_RESEARCH_BUILD_ID"),"phase":"error","stage":"构建中断","reason":type(exc).__name__})
        except (OSError,ValueError):pass
        raise


if __name__=="__main__":main()
