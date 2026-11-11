"""On-demand, read-only stock analysis, independent of trading-duty hours.

The caller supplies an isolated database (optionally containing model bundles).
This module never starts the trading runtime, trains, records forecasts, or orders.
"""
from __future__ import annotations

import math
import re

from .. import db, market
from ..config import now
from . import calendar, chan, data, features, models

LEVELS = {"day": "日线", "60m": "60分钟", "15m": "15分钟", "5m": "5分钟"}
VERSION = "forecast-on-demand-1"
REASONS = {
    "adjustment_unverified": "复权口径尚未核对",
    "stale_completed_bar": "尚未取得最新完成K线",
    "missing_bars": "K线存在缺口",
    "insufficient_completed_bars": "完整K线数量不足",
    "insufficient_bars": "结构计算所需K线不足",
    "calendar_unavailable": "交易日历暂不可用",
    "source_unavailable": "行情来源暂不可用",
    "availability_unknown": "数据可用时点不明",
}


def _explain(reason):
    parts = str(reason or "source_unavailable").split(";")
    return "、".join(dict.fromkeys(REASONS.get(x, "行情校验未通过") for x in parts))


def _table(name):
    return bool(db.q1("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)))


def _model():
    if not _table("forecast_models"):
        return None
    return db.q1("SELECT * FROM forecast_models WHERE status IN ('candidate','active') ORDER BY version DESC LIMIT 1")


def _predict(values, horizon, row):
    empty = {"probability": None, "quantiles": {"p10": None, "p50": None, "p90": None},
             "validated": False, "model_version": row["version"] if row else None,
             "reason": "概率未验证"}
    if not row:
        return empty
    bundle = db.loads(row["artifact"], {})
    if bundle.get("feature_version") != features.VERSION:
        return {**empty, "reason": "feature_version_changed"}
    artifact = bundle.get("models", {}).get(horizon + ":fusion")
    metric = db.loads(row["metrics"], {}).get(horizon + ":fusion", {})
    if not artifact or not metric.get("oos_passed"):
        return empty
    required = artifact.get("features", [])
    if sum(features.finite(values.get(f["name"])) for f in required) < max(1, math.ceil(.8 * len(required))):
        return {**empty, "reason": "feature_coverage_insufficient"}
    try:
        result = models._infer(artifact, values)
    except (KeyError, ValueError, TypeError, IndexError, ZeroDivisionError):
        return {**empty, "reason": "model_bundle_invalid"}
    if not features.finite(result.get("probability")) or not 0 <= result["probability"] <= 1:
        return {**empty, "reason": "model_bundle_invalid"}
    return {**empty, **result, "validated": True, "status": row["status"],
            "reason": "前瞻验证中" if row["status"] != "active" else "active"}


def _quality(frame, structure):
    fq, sq = frame.get("quality", {}), structure.get("quality", {})
    eligible = bool(fq.get("eligible") and sq.get("eligible"))
    return {"eligible": eligible, "reason": None if eligible else
            ";".join(dict.fromkeys(str(q.get("reason") or "source_unavailable") for q in (fq, sq) if not q.get("eligible")))}


def _summary(frames, structures, values, at):
    qualities = {tf: _quality(frames.get(tf, {}), structures.get(tf, {})) for tf in LEVELS}
    descriptions, warnings, directions = [], [], {}
    for tf, label in LEVELS.items():
        result = structures.get(tf, {})
        if not qualities[tf]["eligible"]:
            warnings.append(label + "：" + _explain(qualities[tf]["reason"]) + "，该级别仅供观察。")
            continue
        segments = [s for s in result.get("structures", {}).get("segments", [])
                    if s.get("confirmed") and s.get("level", "segment") == "segment"]
        segment = segments[-1] if segments else None
        direction = segment.get("direction") if segment else None
        directions[tf] = direction
        descriptions.append(label + ("最近确认线段向上" if direction == "up" else
                                    "最近确认线段向下" if direction == "down" else "线段方向尚待确认"))
    known = set(directions.values()) - {None}
    stance = "mixed" if len(known) > 1 else next(iter(known)) if known else "waiting"
    if not qualities["day"]["eligible"] or not qualities["60m"]["eligible"]:
        stance = "waiting"
    text = "；".join(descriptions) + "。" if descriptions else "完整行情尚未取得，暂不能形成可靠的结构判断。"
    if len(known) > 1:
        text += "各级别存在分歧，等待后续确认。"
    elif known:
        text += "该方向描述已确认结构，仍需后续行情验证。"
    if qualities["day"]["eligible"]:
        ma, rsi, ratio, vol = (values.get(k) for k in
                              ("trend_ma20_distance", "momentum_rsi14", "volume_ratio5_20", "risk_vol20"))
        maths = []
        if features.finite(ma):
            maths.append(f"收盘价较20日均线{'高' if ma >= 0 else '低'}{abs(ma)*100:.2f}%")
        if features.finite(rsi):
            maths.append(f"RSI(14)为{rsi*100:.1f}")
        if features.finite(ratio):
            maths.append(f"近5日均量为20日均量的{ratio:.2f}倍")
        if features.finite(vol):
            maths.append(f"近20日对数收益波动率为{vol*100:.2f}%")
        if maths:
            text += "量价与数学观察：" + "，".join(maths) + "。"
    centers = [c for c in structures.get("day", {}).get("structures", {}).get("centers", [])
               if c.get("confirmed")] if qualities["day"]["eligible"] else []
    center = centers[-1] if centers else {}
    if center:
        warnings.append("支撑与阻力为最近确认日线中枢边界；价格可能已在中枢外，不保证到价反转。")
    return {"stance": stance, "summary": text, "support": center.get("low"), "resistance": center.get("high"),
            "updated": at, "quality": {"eligible": all(q["eligible"] for q in qualities.values()),
                                        "reason": None if all(q["eligible"] for q in qualities.values()) else "部分周期行情不足"},
            "warnings": warnings, "timeframe_quality": qualities}


def analyze(code, asof=None, *, feed=None, quote_loader=None, context_loader=None,
            disclosure_loader=None, clock=None):
    """Return a fresh view without persisting predictions or modifying any ledger.

    Explicit asof is for replay/testing only. Production observes each network
    acquisition at its real time, then stamps the analysis after inputs arrive.
    """
    raw = str(code or "").strip().lower()
    if not re.fullmatch(r"(?:(?:sh|sz))?\d{6}", raw):
        raise ValueError("invalid_stock_code")
    code = market.norm_code(raw)
    if not market.is_main_board(code):
        raise ValueError("main_board_stock_required")
    clock = clock or now
    requested = calendar.stamp(asof) if asof is not None else None
    feed = feed or data.fetch
    quote_loader = quote_loader or market.quotes
    context_loader = context_loader or data.context
    disclosure_loader = disclosure_loader or data.disclosure
    frames, structures, failures = {}, {}, {}
    for tf in LEVELS:
        try:
            frames[tf] = feed(code, tf, requested)
            observed = requested or clock()
            structures[tf] = chan.analyze(code, tf, frames[tf].get("rows", []), calendar.iso(observed))
        except (RuntimeError, ValueError, TypeError, KeyError, OSError) as exc:
            failures[tf] = type(exc).__name__
            frames[tf] = {"rows": [], "quality": {"eligible": False, "reason": "source_unavailable"}}
    try:
        quote = dict(quote_loader([code]).get(code) or {})
    except (RuntimeError, ValueError, TypeError, KeyError, OSError):
        quote = {}
    try:
        context = context_loader(requested or clock(), code=code)
    except (RuntimeError, ValueError, TypeError, KeyError, OSError):
        context = {}
    at = requested or clock()
    try:
        disclosure = disclosure_loader(code, at)
    except (RuntimeError, ValueError, TypeError, KeyError, OSError):
        disclosure = None
    f = features.build(frames.get("day", {}).get("rows", []), asof=at, chan=structures,
                       quote=quote, context=context, disclosure=disclosure)
    summary = _summary(frames, structures, f["values"], calendar.iso(at))
    missing = [r["name"] for r in f["coverage"] if not r.get("count") and r.get("id") != "unimplemented"]
    if missing:
        summary["warnings"].append(f"有{len(missing)}类指标缺少可核对的数据，未参与综合判断；可展开运行与研究记录查看。")
    qt = features.stamp(quote.get("source_time") or quote.get("time"))
    quote_valid = bool(qt and qt <= at and features.finite(quote.get("price")) and quote["price"] > 0 and quote.get("source"))
    row = _model()
    predictions = []
    # Intraday targets are never rolled over to the following trading day.
    fresh = quote_valid and 0 <= (at - qt).total_seconds() <= 12
    if fresh and calendar.in_session(at, calendar.official_days(at.year)):
        for horizon in models.HORIZONS:
            group = ("15m", "5m") if horizon.endswith("m") else ("day", "60m")
            due = calendar.due_at(at, horizon, calendar.calendar_window(at.year))
            eligible = all(summary["timeframe_quality"][tf]["eligible"] and structures.get(tf, {}).get("confirmed") for tf in group)
            alignment = chan.align_levels([structures.get(tf, {}) for tf in group])
            eligible = eligible and alignment.get("eligible", False)
            if not due or not eligible:
                continue
            predictions.append({"code": code, "name": quote.get("name", ""), "variant": "fusion", "horizon": horizon,
                                "asof": calendar.iso(at), "due_at": due, "reference_price": quote["price"],
                                "source_time": calendar.iso(qt), "origin": "on_demand_readonly",
                                "quality": {"eligible": True, "reason": None}, "alignment": alignment,
                                **_predict(f["values"], horizon, row)})
    if not predictions and any(s.get("bar_count") for s in structures.values()):
        summary["warnings"].append("当前展示已完成K线的结构分析；没有满足有效报价和期限条件的即时概率预测。")
    symbol = {"code": code, "name": str(quote.get("name") or "")}
    out_structures = {}
    for tf, structure in structures.items():
        small = {k: v[-(180 if k == "klines" else 40):] for k, v in structure.get("structures", {}).items() if k != "revisions"}
        out_structures[code + ":" + tf] = {**{k: structure.get(k) for k in
                                              ("id", "code", "timeframe", "asof", "source_time", "known_at", "confirmed", "config_hash")},
                                         "quality": summary["timeframe_quality"][tf], "structures": small}
    latest = frames.get("day", {}).get("rows", [])
    return {"version": VERSION, "analysis_only": True, "symbol": symbol, "symbols": [symbol], "summary": summary,
            "request": {"status": "ready"}, "status": {"phase": "waiting_signal" if summary["quality"]["eligible"] else "data_blocked",
                "role": "cloud", "covered": 1 if any(x.get("rows") for x in frames.values()) else 0, "requested": 1,
                "last_analysis": calendar.iso(at), "failures": failures},
            "quote": {k: quote.get(k) for k in ("code", "name", "price", "pct", "source_time", "time", "source")} if quote_valid else None,
            "last_completed_close": {"price": latest[-1]["close"], "source_time": latest[-1]["date"]} if latest else None,
            "predictions": predictions, "structures": out_structures,
            "coverage": [{"code": code, "coverage": f["coverage"], "version": f["version"], "hash": f["hash"], "at": calendar.iso(at)}],
            "features": f["values"], "controls": {}, "ledger": {"books": {}}, "decisions": [], "evaluation": {"evaluations": []},
            "model_status": {"versions": [{k: row[k] for k in ("version", "created", "status")}] if row else []}}
