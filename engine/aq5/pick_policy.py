"""竞价和尾盘分别验收冻结的强 A 原榜与第一名，资格仅供后续选股使用。"""
from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from datetime import datetime

from . import db, market, research
from .config import now
from .forecast.calendar import official_days

VERSION = "strict-a-v1"
SESSIONS = ("auction", "close")
MIN_DAYS = research.TEST_DAYS * research.MIN_FOLDS
KEY = "pick_admission:" + VERSION + ":"


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def reference(quote, session, asof):
    stamp = research._quote_stamp(quote)
    if not stamp:
        return None
    at, cutoff = datetime.fromisoformat(stamp), datetime.fromisoformat(asof)
    if at.date() != cutoff.date() or not 0 <= (cutoff - at).total_seconds() <= 300:
        return None
    clock = at.hour * 100 + at.minute
    end_clock = cutoff.hour * 100 + cutoff.minute
    if ((session == "auction" and not 925 <= clock <= end_clock < 1130)
            or (session == "close" and not 1300 <= clock <= end_clock < 1500)):
        return None
    opening = quote.get("open")
    if type(opening) not in (float, int) or not math.isfinite(opening) or opening <= 0:
        return None
    return {"quote_at": stamp, "source": quote.get("source") or ("模拟" if market.MOCK else "腾讯"),
            "open": opening}


def _pending(reason):
    return {"protocol": VERSION, "status": "insufficient", "mode": "observe",
            "reason": reason, "folds": [], "cost_bps": research.COST_BPS}


def current(session, scope=None):
    if session not in SESSIONS or not research.has_schema():
        return _pending("该场次尚无独立样本外验证")
    record = db.q1("SELECT value FROM research_controls WHERE name=?", (KEY + session,))
    policy = db.loads(record["value"], {}) if record else {}
    if not policy or (scope is not None and policy.get("scope") != scope):
        return _pending("当前规则尚无对应的验证记录")
    if policy.get("status") == "passed":
        today = now().date()
        days = official_days(today.year)
        prior = max((d for d in days if d < today.isoformat()), default=None)
        if not prior and days:
            prior = max(official_days(today.year - 1), default=None)
        stamp = str(policy.get("checked_at", ""))[:10]
        if not prior or not prior <= stamp <= today.isoformat():
            return {**policy, "mode": "observe", "status": "insufficient", "reason": "最近交易日尚未复核准入资格"}
    return policy


def _history(session, day):
    # 同日重复补跑仅采用第一场。窗口固定为最近80个信号日，不按结果挑选日期。
    runs = db.q("""SELECT p.id,p.date,rr.report FROM pick_runs p
                   JOIN research_runs rr ON rr.run_id=p.id
                   WHERE p.session=? AND p.date<=? AND rr.version=?
                     AND p.id=(SELECT MIN(q.id) FROM pick_runs q WHERE q.date=p.date AND q.session=p.session)
                   ORDER BY p.date DESC LIMIT 80""", (session, day, research.VERSION))
    if not runs:
        return [], None
    scope = None
    cohorts = {}
    for run in runs:
        signal = db.loads(run["report"], {}).get("selection", {})
        if signal.get("protocol") != VERSION or not signal.get("scope"):
            break
        if scope is None:
            scope = signal["scope"]
        if scope != signal["scope"]:
            break
        codes = signal.get("baseline_codes")
        valid_codes = isinstance(codes, list) and all(isinstance(code, str) for code in codes)
        cohorts[run["id"]] = {"date": run["date"], "codes": codes if valid_codes else [],
                              "quality_blocked": signal.get("quality_blocked") is not False or not valid_codes,
                              "end_5d": None, "returns": {}, "duplicate": False}
    if not cohorts:
        return [], scope
    ids = ",".join("?" for _ in cohorts)
    rows = db.q(f"""SELECT s.run_id,s.code,s.ret_3d,w.end_5d FROM samples s
                    JOIN pick_runs p ON p.id=s.run_id AND p.date=s.date AND p.session=s.session
                    JOIN research_label_windows w ON w.sample_id=s.id
                    WHERE s.run_id IN ({ids}) AND s.evaluated=1 AND w.end_3d>s.date
                    AND w.end_5d>w.end_3d AND w.end_5d<=?""", (*cohorts, day))
    # 最近一个有完整5日标签的信号日以前，缺报价或缺标签必须作为覆盖缺口保留。
    for row in rows:
        cohort = cohorts[row["run_id"]]
        cohort["end_5d"] = max(cohort["end_5d"] or "", row["end_5d"])
        if row["code"] in cohort["codes"]:
            if row["code"] in cohort["returns"]:
                cohort["duplicate"] = True
            if isinstance(row["ret_3d"], (int, float)) and math.isfinite(row["ret_3d"]):
                cohort["returns"][row["code"]] = row["ret_3d"] - research.COST_BPS / 100
    ordered = list(reversed(cohorts.values()))
    mature = max((r["date"] for r in ordered if r["end_5d"]), default="")
    if len(ordered) > 5:
        mature = max(mature, ordered[-6]["date"])
    return [r for r in ordered if r["date"] <= mature], scope


def _stats(rows):
    stats = defaultdict(list)
    for row in rows:
        codes, returns = row["codes"], row["returns"]
        if row["quality_blocked"] or row["duplicate"] or len(set(codes)) != len(codes) or any(c not in returns for c in codes):
            return None
        if codes:
            net = [returns[c] for c in codes]
            for mode, values in (("all_a", net), ("top1", net[:1])):
                stats[mode].append((sum(values) / len(values), sum(x > 0 for x in values) / len(values)))
    if not stats:
        return None
    # 每场等权，单场内等权。多个股票同日上涨只贡献一个独立信号日。
    result = {mode: {"net_pct": sum(r[0] for r in values) / len(values),
                     "win": sum(r[1] for r in values) / len(values)} for mode, values in stats.items()}
    return {**result, "days": len(stats["all_a"]), "different_days": sum(len(r["codes"]) > 1 for r in rows)}


def evaluate(session, day):
    rows, scope = _history(session, day)
    result = {**_pending("新规则的独立信号日或完整未来标签不足"), "scope": scope,
              "mature_signal_dates": len(rows)}
    if len(rows) < research.TRAIN_DAYS + MIN_DAYS:
        return result
    # 以最新成熟日为终点，尾部不舍弃；训练标签必须在该折首日之前结束。
    warmup = research.TRAIN_DAYS + (len(rows) - research.TRAIN_DAYS) % research.TEST_DAYS
    tested = []
    for i in range(warmup, len(rows), research.TEST_DAYS):
        test = rows[i:i + research.TEST_DAYS]
        train = [r for r in rows[:i] if r["end_5d"] and r["end_5d"] < test[0]["date"]
                 and not r["quality_blocked"] and not r["duplicate"] and all(c in r["returns"] for c in r["codes"])]
        fold = _stats(test)
        if len(train) < research.TRAIN_DAYS - 5 or not fold:
            result["reason"] = "分期训练窗口、验证场次或完整强 A 报价路径不足"
            return result
        result["folds"].append({**fold, "train_days": len(train), "train_label_end": max(r["end_5d"] for r in train),
                                "test_start": test[0]["date"], "test_end": test[-1]["date"]})
        tested.extend(test)
    overall = _stats(tested)
    result["validation"] = overall
    if not overall or overall["days"] < MIN_DAYS:
        result["reason"] = f"有效样本外信号日少于{MIN_DAYS}天"
        return result
    folds = result["folds"]
    required = math.ceil(2 * len(folds) / 3)

    def positive(metrics):
        return metrics["net_pct"] > 0 and metrics["win"] > 0.5

    def admitted(mode):
        return (positive(overall[mode]) and positive(folds[-1][mode])
                and sum(positive(f[mode]) for f in folds) >= required)

    def improves(metrics):
        return (metrics["top1"]["net_pct"] > metrics["all_a"]["net_pct"]
                and metrics["top1"]["win"] > metrics["all_a"]["win"])

    if (admitted("top1") and overall["different_days"] >= MIN_DAYS and improves(overall)
            and overall["top1"]["net_pct"] - overall["all_a"]["net_pct"] >= 0.1
            and improves(folds[-1]) and sum(improves(f) for f in folds) >= required):
        result.update(status="passed", mode="top1", reason="强 A 第一名成本后达标，且优于完整强 A 榜")
    elif admitted("all_a"):
        result.update(status="passed", mode="all_a", reason="完整强 A 榜成本后均收益、胜率及分期稳定性达标")
    else:
        result.update(status="rejected", reason="两种规则均未取得正式准入资格，继续留样观察")
    return result


def review():
    research.init()
    stamp = now()
    if (stamp.hour, stamp.minute) < (15, 5):
        return {session: current(session) for session in SESSIONS}
    results = {session: {**evaluate(session, stamp.date().isoformat()),
                         "checked_at": stamp.isoformat(timespec="seconds")} for session in SESSIONS}
    with db.tx() as c:
        for session, receipt in results.items():
            content = {k: v for k, v in receipt.items() if k != "checked_at"}
            digest = fingerprint(content)[:16]
            receipt["evidence_id"] = f"{KEY}{session}:{stamp.date()}:{digest}"
            c.execute("INSERT OR IGNORE INTO research_controls VALUES(?,?)",
                      (receipt["evidence_id"], db.dumps(receipt)))
            c.execute("""INSERT INTO research_controls VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value
                         WHERE json_extract(research_controls.value,'$.checked_at')<=json_extract(excluded.value,'$.checked_at')""",
                      (KEY + session, db.dumps(receipt)))
    return results
