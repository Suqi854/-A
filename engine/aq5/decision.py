"""Manual research decisions. Isolated from simulated accounts and execution."""
from __future__ import annotations

import copy
import threading
from datetime import datetime, timedelta

from . import db, decision_evidence as evidence
from .config import now, CN_TZ

VERSION = "decision-fusion-1"
_contexts = {}
_proposals = {}
_lock = threading.RLock()


def remember(result):
    with _lock:
        for item in result["routes"].values():
            proposal = item.get("proposal")
            if proposal:
                _proposals[proposal["id"]] = copy.deepcopy(proposal)
        while len(_proposals) > 400:
            _proposals.pop(next(iter(_proposals)))
    return result


def quote_time(value):
    text = str(value or "")
    parsed = datetime.strptime(text, "%Y%m%d%H%M%S") if len(text) == 14 and text.isdecimal() else datetime.fromisoformat(text)
    return evidence.stamp(parsed if parsed.tzinfo else parsed.replace(tzinfo=CN_TZ))


def init():
    with db.tx() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS decision_targets(
            id TEXT PRIMARY KEY, code TEXT, route TEXT, created_at TEXT, confirmed_at TEXT,
            previous_id TEXT, body TEXT)""")


def confirmed(code, route):
    if not evidence.exists("decision_targets"):
        return None
    row = db.q1("""SELECT * FROM decision_targets WHERE code=? AND route=?
                   ORDER BY confirmed_at DESC,rowid DESC LIMIT 1""", (code, route))
    return {**db.loads(row["body"]), "confirmed_at": row["confirmed_at"]} if row else None


def structure_context(code, forecast, asof):
    """Only confirmed, known-at-safe geometry; targets remain in the recorded price basis."""
    at = evidence.stamp(asof)
    frames, notes = {}, []
    for tf in ("day", "60m", "15m", "5m"):
        item = forecast.get("structures", {}).get(code + ":" + tf, {})
        try:
            valid = (item.get("code") == code and item.get("timeframe") == tf
                     and item.get("quality", {}).get("eligible") is True
                     and evidence.stamp(item["asof"]) <= at
                     and datetime.fromisoformat(at) - datetime.fromisoformat(evidence.stamp(item["asof"])) <= timedelta(minutes=15))
        except (KeyError, TypeError, ValueError):
            valid = False
        geometry = {}
        for kind in ("centers", "segments", "buy_sell_points"):
            rows = []
            for row in item.get("structures", {}).get(kind, []) if valid else []:
                try:
                    if (row.get("confirmed") is True and evidence.stamp(row["confirmed_at"]) <= at
                            and evidence.stamp(row.get("end") or row.get("at")) <= at):
                        rows.append(row)
                except (ValueError, TypeError, KeyError):
                    continue
            geometry[kind] = sorted(rows, key=lambda r: (r.get("end") or r.get("at"), r["confirmed_at"]))
        frames[tf] = {"eligible": bool(valid), **geometry}
        seg = geometry["segments"][-1] if geometry["segments"] else {}
        notes.append(f"{tf}：" + ("已确认线段" + {"up": "向上", "down": "向下"}.get(seg.get("direction"), "方向待定") if seg else "结构待确认"))
    return frames, notes


def propose(code, route, frames, basis, price, at, claims):
    if not basis or basis.get("verified") is not True or not evidence.finite(price) or price <= 0:
        return None
    a, b = basis.get("a"), basis.get("b")
    if not evidence.finite(a) or a <= 0 or not evidence.finite(b):
        return None
    centers = frames["day"]["centers"]
    bounds = sorted({round((x[k] - b) / a, 2) for x in centers for k in ("low", "high")
                     if evidence.finite(x.get(k)) and (x[k] - b) / a > price})
    supports = sorted({round((x["low"] - b) / a, 2) for x in centers
                       if evidence.finite(x.get("low")) and 0 < (x["low"] - b) / a < price})
    if not bounds or not supports:
        return None
    zone = [bounds[0], bounds[1] if len(bounds) > 1 else bounds[0]]
    proposal = {"code": code, "route": route, "version": VERSION, "created_at": at,
                "price": price, "target_zone": zone, "stop": supports[-1],
                "basis": copy.deepcopy(basis), "evidence_ids": list(claims["evidence_ids"]),
                "branch": claims["branch"], "required_evidence": (["growth", "catalyst"] if route == "value" else
                    ["mainline"] + (["core"] if claims["core"] else ["catalyst"])),
                "structure_ids": [x.get("id") for x in centers],
                "reason": "最近已确认日线中枢边界中，高于参考价的最近压力区间；下方最近中枢下沿为价格失效位。",
                "invalidation": "跌破冻结失效位、适用级别出现新确认卖点，或路线依据被有效证据否定，提示退出。"}
    proposal["id"] = evidence.digest(proposal)
    return proposal


def assess(code, quote, forecast, asof=None, *, basis=None, claims=None, saved=None):
    at = evidence.stamp(asof or now())
    claims = claims if claims is not None else evidence.latest(at).get(code, {})
    routes = evidence.classify(code, claims)
    frames, notes = structure_context(code, forecast, at)
    basis = basis or forecast.get("decision_price_basis")
    q = quote or {}
    price = q.get("price")
    try:
        qt = quote_time(q.get("source_time") or q.get("time"))
        fresh = q.get("code") == code and evidence.finite(price) and price > 0 and 0 <= (datetime.fromisoformat(at) - datetime.fromisoformat(qt)).total_seconds() <= 180
    except (ValueError, TypeError):
        qt, fresh = None, False
    reference = price if fresh else (forecast.get("last_completed_close") or {}).get("price")
    if not fresh and evidence.finite(reference) and basis and basis.get("verified") and evidence.finite(basis.get("a")) and basis["a"] > 0:
        reference = (reference - basis["b"]) / basis["a"]
    for route, item in routes.items():
        tf = "60m" if route == "value" else "15m"
        required = ("day", "60m") if route == "value" else ("60m", "15m")
        geometry_ok = all(frames[t]["eligible"] for t in required)
        points = frames[tf]["buy_sell_points"]
        point = points[-1] if points else {}
        segment = frames[required[0]]["segments"][-1] if frames[required[0]]["segments"] else {}
        window = timedelta(minutes=60 if route == "value" else 15)
        point_new = (bool(point) and datetime.fromisoformat(at) - datetime.fromisoformat(evidence.stamp(point["confirmed_at"])) <= window
                     and datetime.fromisoformat(at) - datetime.fromisoformat(evidence.stamp(point["at"])) <= window)
        target = (saved or {}).get(route) if saved is not None else confirmed(code, route)
        holding_valid = False
        if target:
            required_claims = [k for k in target.get("required_evidence", []) if k not in ("cooling", "hot_money")]
            item["invalid"] = item["invalid"] or any(claims.get(k, {}).get("value") is False for k in required_claims)
            holding_valid = all(claims.get(k, {}).get("value") is True for k in required_claims)
        proposal = propose(code, route, frames, basis, reference, at, item)
        item.update(structure_evidence=notes, proposal=proposal, confirmed_target=target,
                    target_zone=(target or proposal or {}).get("target_zone"),
                    trigger=f"{tf}新确认买点，上级线段向上；路线证据完整、报价有效且位于已确认目标下方。",
                    hold="路线逻辑与结构有效则继续跟踪，不设固定持有天数；确认研究目标不代表已经持仓。",
                    invalidation=(target or proposal or {}).get("invalidation", "路线证据否定或已确认结构失效时明确提示退出。"))
        state, verdict = "watch", "观察，等待条件确认"
        comparable = bool(target and basis and basis.get("verified") and target.get("basis", {}).get("date") == basis.get("date"))
        # Never compare pre-corporate-action raw levels with a later basis without explicit reconfirmation.
        if target and basis and basis.get("verified"):
            old = target.get("basis", {})
            comparable = all(old.get(k) == basis.get(k) for k in ("a", "b"))
        after = target and qt and qt >= target["confirmed_at"]
        new_sell = (target and geometry_ok and point.get("side") == "sell"
                    and point.get("confirmed_at", "") >= target["confirmed_at"]
                    and point.get("at", "") >= target["confirmed_at"])
        if item["invalid"] or new_sell or (target and after and fresh and comparable and price <= target["stop"]):
            state, verdict = "exit", "逻辑或结构失效：持有者退出，未持有者撤销观察"
        elif target and after and fresh and comparable and price >= target["target_zone"][0]:
            state, verdict = "take_profit", "到达已确认目标区域：持有者兑现，不等待结构转弱"
        elif target and holding_valid and geometry_ok and fresh and comparable:
            state, verdict = "track", "逻辑与结构继续跟踪；持有者按冻结目标管理"
            if (item["eligible"] and point_new and point.get("side") == "buy" and segment.get("direction") == "up"
                    and target["stop"] < price < target["target_zone"][0]):
                state, verdict = "participate", "参与条件已确认；由你决定是否参与"
        elif item["eligible"] and geometry_ok and point_new and point.get("side") == "buy" and segment.get("direction") == "up":
            state, verdict = "await_target", "结构条件已出现，等待你确认目标及有效报价"
        if target and not comparable:
            verdict += "；原目标保留，当前价格口径待重新核对"
        if item["missing"]:
            verdict += "；待补：" + "、".join(item["missing"])
        item.update(state=state, verdict=verdict, quote_fresh=fresh, quote_time=qt)
    return {"version": VERSION, "code": code, "asof": at, "routes": routes,
            "chart_basis": basis, "chart_reference": forecast.get("last_completed_close"),
            "chart_centers": frames["day"]["centers"][-1:],
            "manual_only": True, "source_note": "缠论结构按可得时间确认；研究规则尚非收益验证。"}


def load_forecast(code):
    """Reuse the actual read-only multi-timeframe service and its checked adjustment bundle."""
    from .forecast import service, data
    result = service.analyze(code)
    try:
        bundle = data.adjustment_factors(code)
        days = bundle["evidence"]["days"]
        day = max(days)
        conversion = days[day]
        if result.get("data_quality", {}).get("day", {}).get("quality", {}).get("eligible"):
            result["decision_price_basis"] = {"verified": True, "a": conversion["a"], "b": conversion["b"], "date": day,
                                               "source": "已核验原价与复权价映射", "observed_at": bundle["evidence"]["observed_at"]}
    except (KeyError, ValueError, RuntimeError):
        result["decision_price_basis"] = {"verified": False}
    return result


def view(code, *, forecast=None):
    code = evidence.code_of(code)
    forecast = load_forecast(code) if forecast is None else forecast
    with _lock:
        _contexts[code] = copy.deepcopy(forecast)
        if len(_contexts) > 100:
            _contexts.pop(next(iter(_contexts)))
    return remember(assess(code, forecast.get("quote"), forecast))


def status(code):
    from . import market
    code = evidence.code_of(code)
    with _lock:
        context = copy.deepcopy(_contexts.get(code, {}))
    return remember(assess(code, market.quotes_recent([code]).get(code), context))


def confirm_target(body):
    """Recompute before confirmation, compare-and-swap, and never overwrite prior versions."""
    code = evidence.code_of(body.get("code"))
    route = body.get("route")
    if route not in evidence.ROUTES:
        raise ValueError("未知研究路线")
    current = view(code)["routes"][route]
    identity = body.get("proposal_id") or (body.get("proposal") or {}).get("id")
    candidate = _proposals.get(identity)
    if not isinstance(candidate, dict) or candidate.get("code") != code or candidate.get("route") != route:
        raise ValueError("目标提案已过期或与股票/路线不匹配，请刷新")
    if body.get("proposal") is not None and body["proposal"] != candidate:
        raise ValueError("目标提案被修改")
    age = datetime.fromisoformat(evidence.stamp(now())) - datetime.fromisoformat(evidence.stamp(candidate.get("created_at")))
    if not timedelta(0) <= age <= timedelta(minutes=15):
        raise ValueError("提案已过期，请重新分析")
    proposal = current.get("proposal")
    keys = ("target_zone", "stop", "evidence_ids", "structure_ids", "version", "reason", "invalidation", "branch", "required_evidence")
    if (not proposal or any(candidate.get(k) != proposal.get(k) for k in keys)
            or any(candidate.get("basis", {}).get(k) != proposal["basis"].get(k) for k in ("a", "b"))
            or current["invalid"]):
        raise ValueError("依据或目标已变化，请刷新后重新确认")
    init()
    with db.tx() as c:
        old = c.execute("SELECT id FROM decision_targets WHERE code=? AND route=? ORDER BY confirmed_at DESC,rowid DESC LIMIT 1", (code, route)).fetchone()
        if old and old["id"] == identity:
            return confirmed(code, route)
        if (old["id"] if old else None) != body.get("previous_id"):
            raise ValueError("其他页面已更新目标，请刷新后再确认")
        c.execute("INSERT INTO decision_targets VALUES(?,?,?,?,?,?,?)",
                  (identity, code, route, candidate["created_at"], evidence.stamp(now()),
                   body.get("previous_id"), db.dumps(candidate)))
    return confirmed(code, route)


def merge(analysis, fusion):
    """Replace text/values inside the existing analysis layout; leave base evaluator untouched."""
    out = copy.deepcopy(analysis)
    out["fusion"] = fusion
    items = list(fusion["routes"].values())
    primary = next((x for x in items if x.get("confirmed_target") and x["state"] in ("exit", "take_profit")),
                   next((x for x in items if x.get("confirmed_target")),
                        next((x for x in items if x["eligible"]), items[0])))
    out["rating"] = {"exit": "退出", "take_profit": "兑现", "participate": "条件确认", "track": "持续跟踪",
                     "await_target": "待确认目标", "watch": "观察"}[primary["state"]]
    out["tone"] = "avoid" if primary["state"] == "exit" else "hold"
    if primary["state"] == "exit" and not primary.get("confirmed_target"):
        out["rating"] = "不符合路线条件"
    out["advice"] = "；".join(x["label"] + "：" + x["verdict"] for x in items)
    out["reasons"] = [*out.get("reasons", []), *primary["structure_evidence"], *primary["evidence"]]
    chosen = primary.get("confirmed_target") or primary.get("proposal") or {}
    out["fusion"]["chart_plan_compatible"] = bool(chosen and fusion.get("chart_basis") and
        all(chosen.get("basis", {}).get(k) == fusion["chart_basis"].get(k) for k in ("a", "b")))
    pl = out["plan"]
    pl.update(hold_days=None, target1=(chosen.get("target_zone") or [None, None])[0],
              target2=(chosen.get("target_zone") or [None, None])[1], stop=chosen.get("stop"),
              rr=None, target1_pct=None, target2_pct=None, stop_pct=None,
              buy_zone=[None, None], supports=[chosen["stop"]] if chosen else [],
              resistances=chosen.get("target_zone", []),
              rules=[f"{x['label']}：{x['verdict']}。触发：{x['trigger']}。失效：{x['invalidation']}" for x in items]
              + ["持有不设固定天数；目标由你确认。点击目标价格查看两条路线的提案与确认记录。", fusion["source_note"]])
    return out
