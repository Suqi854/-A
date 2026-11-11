"""Real, bounded public research acquisition shared by local and isolated cloud jobs.

No account or order actions. Collected facts describe the present observation;
they never retroactively create historical selections.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from . import db, market, research, decision, decision_evidence as evidence
from .config import now

VERSION = "decision-collection-1"
MAX_FINANCIAL_CODES = 100


def _public(report):
    """Keep provenance, never expose local paths or raw provider bodies to the UI."""
    if isinstance(report, dict):
        return {k: _public(v) for k, v in report.items() if k not in ("raw_path", "storage_dir", "raw_body")}
    if isinstance(report, list):
        return [_public(v) for v in report]
    return report


def _pool(snapshot):
    valid = []
    for row in snapshot:
        code = row.get("code", "")
        if (market.is_main_board(code) and "ST" not in str(row.get("name", "")).upper()
                and "退" not in str(row.get("name", "")) and not row.get("suspended")
                and evidence.finite(row.get("price")) and row["price"] > 0):
            valid.append(row)
    return sorted(valid, key=lambda r: (-(r.get("amount") if evidence.finite(r.get("amount")) else 0), r["code"]))


def _ingest(report):
    inserted, failures, ids = 0, [], {}
    for row in report.get("records", []):
        try:
            result = evidence.import_records([row], automated=True)
            inserted += result["inserted"]
            ids.setdefault(row["code"], []).extend(result["ids"])
        except (ValueError, TypeError) as exc:
            failures.append({"code": row.get("code"), "kind": row.get("kind"), "reason": str(exc)})
    return {"inserted": inserted, "rejected": failures, "active_ids": ids}


def collect(snapshot, codes, *, focus=False):
    from . import decision_growth, decision_emotion
    db.init()
    jobs = {"growth": lambda: decision_growth.collect(codes),
            "emotion": lambda: decision_emotion.collect(snapshot, **({"focus_codes": codes} if focus else {}))}
    reports = {}
    with ThreadPoolExecutor(max_workers=2) as executor:
        pending = {key: executor.submit(job) for key, job in jobs.items()}
        for key, future in pending.items():
            try:
                report = future.result()
                report["ingestion"] = _ingest(report)
            except (RuntimeError, ValueError, OSError, TypeError, KeyError) as exc:
                report = {"records": [], "coverage": {"status": "unavailable"},
                          "failures": [{"reason": type(exc).__name__ + ":" + str(exc)[:180]}]}
            reports[key] = report
    active = {}
    for report in reports.values():
        for code, ids in report.get("ingestion", {}).get("active_ids", {}).items():
            active.setdefault(code, []).extend(ids)
    evidence.record_observation(codes if focus else [row["code"] for row in snapshot], active)
    return {"version": VERSION, "observed_at": evidence.stamp(now()), "sources": reports,
            "scope": {"financial_requested": len(codes), "snapshot_count": len(snapshot),
                      "financial_codes": codes, "full_market": False,
                      "selection": "沪深主板非ST公开快照，按成交额优先的最多100股财务核对；主线情绪以采集器实际覆盖为准"}}


def screen():
    db.init()
    try:
        snapshot = market.snapshot()
    except (RuntimeError, OSError):
        snapshot = []
    pool = _pool(snapshot)
    codes = [r["code"] for r in pool[:MAX_FINANCIAL_CODES]]
    collection = collect(pool, codes) if pool else {"version":VERSION,"observed_at":evidence.stamp(now()),
        "sources": {"market":{"coverage":{"status":"unavailable"},"failures":[{"reason":"public_snapshot_unavailable"}]}},
        "scope":{"financial_requested":0,"snapshot_count":0,"financial_codes":[],"full_market":False}}
    at = now()
    report = research.preview(snapshot=pool, asof=evidence.stamp(at), include_decision=True)
    report["collection"] = _public({**collection, "sources": {k: {x: y for x, y in v.items() if x != "records"}
                                                                for k, v in collection["sources"].items()}})
    report["scope"] = collection["scope"]
    queried = set(codes) | {r["code"] for group in report["routes"].values() for r in group["candidates"] + group["watch"]}
    report["source_evidence"] = {code: claims for code, claims in evidence.latest(at).items() if code in queried}
    for key, route in report["routes"].items():
        route["note"] += (f" 本次从{len(pool)}股公开快照中按成交额优先核对{len(codes)}股财务，非全市场筛选完成。"
                          if key == "value" else " 本次仅核对部分强势板块与成分，非全市场筛选完成；龙虎榜仅作加分。")
    return report


def analysis(code):
    from . import analysis as base
    code = evidence.code_of(market.norm_code(code))
    if not market.is_main_board(code):
        raise ValueError("当前融合分析仅支持沪深主板")
    db.init()
    try:
        snapshot = market.snapshot()
    except (RuntimeError, OSError):
        snapshot = []
    # Emotion needs the common market context; the financial query is this stock only.
    collection = collect(_pool(snapshot), [code], focus=True)
    raw = base.analyze(code)
    fusion = decision.view(code)
    result = decision.merge(raw, fusion)
    result["kline"] = market.kline_with_live(code, 250)
    result["analysis_time"] = evidence.stamp(now())
    result["source_evidence"] = evidence.latest(now()).get(code, {})
    lhb = [x for x in collection["sources"].get("emotion", {}).get("proxy_metrics", []) if x.get("code") == code]
    details = result.setdefault("main_force", {}).setdefault("evidence", [])
    if not lhb:
        details.append("龙虎榜：本次未取得该股可核对的席位详情；不据此否定主线与结构，也不推断没有游资参与。")
    for item in lhb:
        details.append(f"龙虎榜 {item['trade_date']}：非机构营业部席位净额 {item['non_institution_net']/1e8:+.2f}亿元；"
                       "仅为席位参与代理，实际游资身份未核实，作为加分参考。")
        for seat in item.get("seats", []):
            label = {"brokerage_seat_proxy":"营业部", "institution_or_connect":"机构/股通", "unknown":"类别未知"}[seat["category"]]
            details.append(f"龙虎榜席位（{label}）{seat['name']}：买入{seat['buy']/1e4:.1f}万，卖出{seat['sell']/1e4:.1f}万，净额{seat['net']/1e4:+.1f}万。")
        details.append("龙虎榜来源：东方财富公开披露转载；" + item.get("note", ""))
    result["collection"] = _public({**collection, "sources": {k: {x: y for x, y in v.items() if x != "records"}
                                                                for k, v in collection["sources"].items()}})
    return result
