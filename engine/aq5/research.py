"""冻结信号快照、可解释候选榜和日期级影子对照。"""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from datetime import datetime

from . import db, fundamentals, market
from .config import CN_TZ, now

VERSION = 1
FACTORS = {
    "short_strength": "相对市场强度与成交额位置",
    "industry_leadership": "行业扩散与板块内相对强度和成交额位置",
    "disclosure_quality": "披露可用的盈利、经营现金流和资产负债质量",
}
TOP_K = 10
COST_BPS = 30
TRAIN_DAYS = 15
TEST_DAYS = 5
MIN_FOLDS = 3


def init() -> None:
    with db.tx() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS research_runs(
            run_id INTEGER PRIMARY KEY, asof TEXT, version INTEGER, report TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS research_samples(
            run_id INTEGER, code TEXT, baseline_score REAL, baseline_rank INTEGER, factors TEXT,
            PRIMARY KEY(run_id, code))""")
        c.execute("""CREATE TABLE IF NOT EXISTS research_label_windows(
            sample_id INTEGER PRIMARY KEY, end_3d TEXT, end_5d TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS research_factor_state(
            id TEXT, version INTEGER, enabled INTEGER, changed_at TEXT, evidence TEXT,
            PRIMARY KEY(id, version))""")
        c.execute("""CREATE TABLE IF NOT EXISTS research_controls(
            name TEXT PRIMARY KEY, value TEXT)""")


def has_schema() -> bool:
    return bool(db.q1("SELECT 1 FROM sqlite_master WHERE type='table' AND name='research_runs'"))


def timestamp() -> str:
    return now().astimezone(CN_TZ).isoformat(timespec="seconds")


def _finite(x) -> bool:
    return type(x) in (float, int) and math.isfinite(x)


def _ranks(rows: list[dict], key: str) -> dict:
    from .learn import _rank
    valid = [r for r in rows if _finite(r.get(key))]
    n = len(valid)
    return {r["code"]: 100 * v / (n - 1) if n > 1 else 50
            for r, v in zip(valid, _rank([r[key] for r in valid]))}


def screen(snapshot: list[dict], asof: str, industries: dict) -> tuple[dict, dict]:
    """行业强弱与行情覆盖均基于这份快照，避免从另外时点拼接市场数据。"""
    quoted = [r for r in snapshot if _finite(r.get("price")) and r["price"] > 0
              and _finite(r.get("pct")) and r.get("prev") and not r.get("suspended")]
    br = market.breadth(quoted)
    features, candidates = defaultdict(dict), []
    rs = _ranks(quoted, "pct")
    amt = _ranks([r for r in quoted if (r.get("amount") or 0) > 0], "amount")
    groups = defaultdict(list)
    for r in quoted:
        if (industry := industries.get(r["code"])):
            groups[industry].append(r)
    industry_score, expansion = {}, {}
    for rows in groups.values():
        if len(rows) < 3:
            continue
        up_ratio = sum(r["pct"] > 0 for r in rows) / len(rows)
        local_rs = _ranks(rows, "pct")
        local_amt = _ranks([r for r in rows if (r.get("amount") or 0) > 0], "amount")
        for code, amount_rank in local_amt.items():
            industry_score[code] = round(0.50 * local_rs[code] + 0.25 * amount_rank + 25 * up_ratio, 2)
            expansion[code] = round(up_ratio, 3)
    for r in quoted:
        code = r["code"]
        if code not in amt:
            continue
        rel_strength = r["pct"] - br["median_pct"]
        strength = round(0.7 * rs[code] + 0.3 * amt[code], 2)
        features[code]["short_strength"] = strength
        lead = industry_score.get(code)
        if lead is not None:
            features[code]["industry_leadership"] = lead
        if r["pct"] >= market.limit_pct(code, r.get("name")) * 100 - 0.3:
            continue
        candidates.append({"code": code, "name": r.get("name"), "pct": r["pct"],
                           "relative_pct": round(rel_strength, 2), "amount_rank": round(amt[code], 2),
                           "industry": industries.get(code), "industry_up_ratio": expansion.get(code),
                           "leadership_score": lead, "short_strength": strength})
    candidates.sort(key=lambda r: (-r["short_strength"], r["code"]))
    leaders = sorted((r for r in candidates if r["leadership_score"] is not None),
                     key=lambda r: (-r["leadership_score"], r["code"]))
    value, quality = fundamentals.screen(snapshot, asof)
    for code, factor in quality.items():
        features[code]["disclosure_quality"] = factor
    return {"version": VERSION, "asof": asof,
            "short_term": {
                "status": "partial" if quoted else "unavailable", "snapshot_count": len(snapshot),
                "quote_count": len(quoted), "amount_count": len(amt), "industry_count": len(industry_score),
                "vwap_count": sum(_finite(r.get("avg")) and r["avg"] > 0 for r in quoted),
                "breadth": {k: br[k] for k in ("up", "down", "flat", "limit_up", "limit_down", "median_pct")},
                "candidates": candidates[:TOP_K], "leaders": leaders[:TOP_K],
                "data_note": "当期行情快照指标。缺失均价不补算，未计算炸板和连板历史指标。",
            }, "value_quality": value}, dict(features)


def _quote_stamp(row: dict) -> str | None:
    raw = str(row.get("quote_time") or row.get("time") or "")
    try:
        if len(raw) == 14 and raw.isascii() and raw.isdecimal():
            t = datetime.strptime(raw, "%Y%m%d%H%M%S")
        elif len(raw) >= 19:
            t = datetime.fromisoformat(raw)
        else:
            return None
        return (t if t.tzinfo else t.replace(tzinfo=CN_TZ)).astimezone(CN_TZ).isoformat(timespec="seconds")
    except ValueError:
        return None


def preview(snapshot: list[dict] | None = None, asof: str | None = None, *, include_decision=False) -> dict:
    """只读研究视图。行情时标由供源给出，当前生成时刻不作为行情时标。"""
    from . import sectors

    supplied = snapshot is not None
    if not supplied:
        try:
            snapshot = market.snapshot()
        except RuntimeError:
            snapshot = []
    generated_at = timestamp()
    cutoff = fundamentals.stamp(asof) if asof is not None else generated_at
    if cutoff > generated_at:
        raise ValueError("预览截止时间不能晚于当前时间")
    rows, times = [], []
    for row in snapshot:
        quote_at = _quote_stamp(row)
        if quote_at and quote_at > cutoff or not quote_at and not supplied and asof is not None:
            continue
        rows.append(row)
        if quote_at:
            times.append(quote_at)
    earliest, latest = (min(times), max(times)) if times else (None, None)
    industries = {}
    if db.q1("SELECT 1 FROM sqlite_master WHERE type='table' AND name='stock_sector'"):
        industries = {code: industry for code, (industry, _) in sectors._map().items()}
    # 披露时点不晚于已标时行情中最早的时刻。无行情时标时，仅说明按何时已知的公告计算。
    report, _ = screen(rows, earliest or cutoff, industries)
    status = "unknown" if not times else "known" if len(times) == len(rows) else "partial"
    report["value_quality"]["quote_time_status"] = status
    if include_decision:
        from . import decision_evidence
        # Current research may use a newly obtained disclosure, while retaining
        # each quote's original timestamp. This is never a historical signal.
        report["routes"] = decision_evidence.screen(rows, cutoff)
    return {**report, "kind": "research_preview", "not_new_selection": True, "asof": latest,
            "generated_at": generated_at, "quote_time_status": status, "quote_time_rows": len(times),
            "quote_time_min": earliest, "active_factors": sorted(active_factors())}


def adjustment(factors: dict, active: set[str]) -> float:
    # 每个正式启用因子的影响上限为 ±3 分，缺失保持原评分。
    return sum((factors[key] - 50) * 0.06 for key in active if key in factors)


def active_factors() -> set[str]:
    if not has_schema():
        return set()
    return {r["id"] for r in db.q("SELECT id FROM research_factor_state WHERE version=? AND enabled=1", (VERSION,))
            if r["id"] in FACTORS}


def save_run(conn, run_id: int, asof: str, report: dict, evaluated: list[dict], features: dict) -> None:
    conn.execute("INSERT INTO research_runs VALUES(?,?,?,?)", (run_id, asof, VERSION, db.dumps(report)))
    conn.executemany("INSERT INTO research_samples VALUES(?,?,?,?,?)",
                     [(run_id, e["code"], e["baseline_score"], e["baseline_rank"], db.dumps(features.get(e["code"], {})))
                      for e in evaluated])


def for_runs(run_ids: list[int]) -> dict:
    if not run_ids or not has_schema():
        return {}
    placeholders = ",".join("?" for _ in run_ids)
    return {r["run_id"]: db.loads(r["report"], {})
            for r in db.q(f"SELECT run_id, report FROM research_runs WHERE run_id IN ({placeholders})", run_ids)}


def date_folds(rows: list[dict], train_days: int = TRAIN_DAYS, test_days: int = TEST_DAYS):
    days = sorted({r["date"] for r in rows})
    for i in range(train_days, len(days) - test_days + 1, test_days):
        test_dates = set(days[i:i + test_days])
        test_start = days[i]
        training = [r for r in rows if r["date"] < test_start and r["end_5d"] and r["end_5d"] < test_start]
        if len({r["date"] for r in training}) < max(1, train_days - 5):
            continue
        yield training, [r for r in rows if r["date"] in test_dates]


def shadow_report(factor_id: str | None = None) -> dict:
    if factor_id is not None and factor_id not in FACTORS:
        raise ValueError("未知影子因子")
    rows = db.q("""SELECT s.run_id,s.date,s.session,s.code,s.ret_3d,w.end_5d,
                    r.baseline_score,r.baseline_rank,r.factors
                    FROM research_samples r JOIN research_runs rr ON rr.run_id=r.run_id
                    JOIN samples s ON s.run_id=r.run_id AND s.code=r.code
                    LEFT JOIN research_label_windows w ON w.sample_id=s.id
                    WHERE s.session IN ('auction','close','manual')
                    AND rr.version=? AND s.date >= (SELECT COALESCE(MIN(date),'') FROM
                      (SELECT DISTINCT s2.date AS date FROM research_label_windows w2
                       JOIN samples s2 ON s2.id=w2.sample_id ORDER BY s2.date DESC LIMIT 80))
                    ORDER BY s.date,s.run_id,s.code""", (VERSION,)) if has_schema() else []
    for row in rows:
        row["factors"] = db.loads(row["factors"], {})
    folds = list(date_folds(rows))
    results = {}
    for key in ([factor_id] if factor_id else FACTORS):
        session_rows, session_valid = Counter(), Counter()
        for row in rows:
            session_rows[row["session"]] += 1
            session_valid[row["session"]] += int(key in row["factors"])
        evidence = []
        for train, test in folds:
            groups = defaultdict(list)
            for row in test:
                if row["session"] != "manual":
                    groups[row["run_id"]].append(row)
            base_ret, new_ret = [], []
            for g in groups.values():
                if len(g) < 2 * TOP_K or sum(key in r["factors"] for r in g) < 0.8 * len(g):
                    continue
                base = sorted(g, key=lambda r: r["baseline_rank"])[:TOP_K]
                proposed = sorted(g, key=lambda r: (-(r["baseline_score"] + adjustment(r["factors"], {key})), r["baseline_rank"]))[:TOP_K]
                if any(r["ret_3d"] is None or not r["end_5d"] for r in base + proposed):
                    continue
                base_ret.extend(r["ret_3d"] - COST_BPS / 100 for r in base)
                new_ret.extend(r["ret_3d"] - COST_BPS / 100 for r in proposed)
            if base_ret:
                evidence.append({"train_days": len({r["date"] for r in train}),
                                 "purged_before": min(r["date"] for r in test),
                                 "test_start": min(r["date"] for r in test), "test_end": max(r["date"] for r in test),
                                 "n": len(base_ret), "baseline": round(sum(base_ret) / len(base_ret), 4),
                                 "candidate": round(sum(new_ret) / len(new_ret), 4)})
        deltas = [x["candidate"] - x["baseline"] for x in evidence]
        passed = (len(deltas) >= MIN_FOLDS and sum(x["n"] for x in evidence) >= 150
                  and sum(deltas) / len(deltas) >= 0.10 and sum(d > 0 for d in deltas) >= math.ceil(2 * len(deltas) / 3))
        results[key] = {"title": FACTORS[key], "eligible": passed, "folds": evidence,
                        "coverage": {k: {"observed": session_valid[k], "samples": n} for k, n in session_rows.items()}}
    return {"version": VERSION, "status": "shadow", "factors": results, "cost_bps_assumed": COST_BPS,
            "baseline": "同一运行冻结的原评分前10，按选出参考价至第3个完整收盘价比较。",
            "limits": "影子报价路径按固定成本估算，不是可成交回测或实际收益。"}


def promote_factor(factor_id: str, version: int, *, enabled: bool = True) -> dict:
    if version != VERSION or factor_id not in FACTORS:
        raise ValueError("未知或过期的影子因子版本")
    init()
    evidence = shadow_report(factor_id)["factors"][factor_id] if enabled else {}
    if enabled and not evidence["eligible"]:
        raise ValueError("影子验证未通过，不能启用")
    with db.tx() as c:
        if enabled and c.execute("""SELECT 1 FROM research_factor_state
                                  WHERE version=? AND enabled=1 AND id<>?""", (VERSION, factor_id)).fetchone():
            raise ValueError("当前每次只允许启用一个已独立验证的因子")
        c.execute("""INSERT INTO research_factor_state VALUES(?,?,?,?,?)
                     ON CONFLICT(id,version) DO UPDATE SET enabled=excluded.enabled,
                     changed_at=excluded.changed_at,evidence=excluded.evidence""",
                  (factor_id, version, int(enabled), timestamp(), db.dumps(evidence)))
    return {"id": factor_id, "version": version, "enabled": enabled}
