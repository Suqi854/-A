"""Dated research evidence and two independent watch lists; never places orders."""
from __future__ import annotations

import hashlib
import math
import re
from datetime import datetime
from urllib.parse import urlsplit

from . import db
from .config import CN_TZ, now

KINDS = {"growth", "catalyst", "mainline", "core", "hot_money", "cooling", "invalid"}
LABELS = {"growth": "业绩加速", "catalyst": "催化", "mainline": "主线", "core": "核心地位",
          "hot_money": "游资参与", "cooling": "核心冷却", "invalid": "逻辑失效"}
ROUTES = {"value": "中短线价值 · 业绩催化", "emotion": "超短线情绪 · 主线核心"}


def stamp(value):
    t = datetime.fromisoformat(str(value))
    if t.utcoffset() is None:
        raise ValueError("证据时间必须包含时区")
    return t.astimezone(CN_TZ).isoformat(timespec="seconds")


def digest(value):
    return hashlib.sha256(db.dumps(value).encode()).hexdigest()


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def code_of(value):
    code = str(value).lower()
    if not re.fullmatch(r"(?:sh|sz)\d{6}", code):
        raise ValueError("需要完整沪深股票代码")
    return code


def exists(table):
    return bool(db.q1("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)))


def init():
    with db.tx() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS decision_evidence(
            id TEXT PRIMARY KEY, code TEXT, kind TEXT, known_at TEXT, expires_at TEXT, body TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS decision_observations(
            id INTEGER PRIMARY KEY, code TEXT, known_at TEXT, active_ids TEXT)""")


def record_observation(codes, active_ids):
    """A failed/partial new observation cannot silently revive an older automatic claim."""
    init()
    at = stamp(now())
    with db.tx() as c:
        c.executemany("INSERT INTO decision_observations(code,known_at,active_ids) VALUES(?,?,?)",
                      [(code_of(code), at, db.dumps(sorted(active_ids.get(code, [])))) for code in sorted(set(codes))])


def import_records(records, *, automated=False):
    """Import human-reviewed public evidence. A URL is provenance, not online verification."""
    if not isinstance(records, list) or not 0 < len(records) <= 1000:
        raise ValueError("需要1至1000条已核对证据")
    prepared = []
    at = stamp(now())
    for raw in records:
        if not isinstance(raw, dict) or raw.get("kind") not in KINDS:
            raise ValueError("证据类别不支持")
        code, kind = code_of(raw.get("code")), raw["kind"]
        published, available, expiry = (stamp(raw.get(k)) for k in ("published_at", "available_at", "expires_at"))
        if not published <= available <= at < expiry:
            raise ValueError("发布时间/可得时间/有效期无效")
        source = str(raw.get("source_url", ""))
        url = urlsplit(source)
        if len(source) > 2000 or url.scheme != "https" or not url.hostname or url.username or url.password or url.path in ("", "/"):
            raise ValueError("需要具体的公开HTTPS来源页面")
        note = str(raw.get("note", "")).strip()
        if not note or len(note) > 2000 or type(raw.get("value")) is not bool:
            raise ValueError("需要证据说明和明确的true/false判断")
        metrics = raw.get("metrics", {})
        if kind == "growth":
            keys = ("revenue_yoy", "prior_revenue_yoy", "profit_yoy", "prior_profit_yoy")
            if not isinstance(metrics, dict) or not all(finite(metrics.get(k)) for k in keys):
                raise ValueError("业绩加速需提供同口径本期/上期营收与净利同比")
            if raw.get("basis") != "comparable_quarter_yoy":
                raise ValueError("需要同口径季度同比，不能用累计或TTM冒充单季加速")
            accelerating = (metrics["revenue_yoy"] > max(0, metrics["prior_revenue_yoy"])
                            and metrics["profit_yoy"] > max(0, metrics["prior_profit_yoy"]))
            if raw["value"] != accelerating:
                raise ValueError("业绩加速判断与数值不一致")
            metrics = {k: metrics[k] for k in keys}
        else:
            metrics = {}
        provenance = None
        if automated:
            provenance = raw.get("provenance")
            if (not isinstance(provenance, dict) or not re.fullmatch(r"[a-f0-9]{64}", str(provenance.get("raw_sha256", "")))
                    or not all(isinstance(provenance.get(k), str) and provenance[k].strip() for k in ("provider", "rule_version", "method"))):
                raise ValueError("自动证据必须包含原响应哈希、来源和规则版本")
            retrieved = stamp(provenance.get("retrieved_at"))
            if retrieved > at:
                raise ValueError("原响应抓取时间不能晚于记录时间")
            available = max(available, retrieved)
        body = {"code": code, "kind": kind, "value": raw["value"], "note": note,
                "published_at": published, "available_at": available, "recorded_at": at,
                "known_at": max(available, at), "expires_at": expiry, "source_url": source,
                "metrics": metrics, "review": "公开源自动采集与规则推断；不等同官方结论" if automated else "人工核对导入；未在线核验"}
        if provenance is not None:
            keep = ("provider", "raw_sha256", "retrieved_at", "rule_version", "method", "period_end",
                    "comparison_period", "input_periods", "raw_values", "official_verified", "trade_date", "identity")
            body["provenance"] = {k: provenance[k] for k in keep if k in provenance}
            if len(db.dumps(body["provenance"])) > 20000:
                raise ValueError("自动证据追溯信息过大")
        # Idempotency does not backdate the first time this installation knew the evidence.
        semantic = {k: v for k, v in body.items() if k not in ("recorded_at", "known_at")}
        if automated:
            semantic.pop("available_at", None)
            semantic["provenance"] = {k: provenance[k] for k in ("provider", "rule_version", "method")}
        identity = digest(semantic)
        prepared.append((identity, code, kind, body["known_at"], expiry, db.dumps({**body, "id": identity})))
    init()
    inserted = 0
    with db.tx() as c:
        for row in prepared:
            prior = c.execute("SELECT id FROM decision_evidence WHERE code=? AND kind=? AND known_at=?",
                              (row[1], row[2], row[3])).fetchall()
            if prior and any(p["id"] != row[0] for p in prior):
                raise ValueError("同一时点的同类证据冲突")
            inserted += c.execute("INSERT OR IGNORE INTO decision_evidence VALUES(?,?,?,?,?,?)", row).rowcount
    return {"inserted": inserted, "received": len(records), "source_verified_online": False,
            "ids": [row[0] for row in prepared]}


def latest(asof):
    at = stamp(asof)
    out = {}
    scopes = {}
    if exists("decision_observations"):
        for row in db.q("SELECT * FROM decision_observations WHERE known_at<=? ORDER BY known_at DESC,id DESC", (at,)):
            scopes.setdefault(row["code"], {"ids":set(db.loads(row["active_ids"], [])), "known_at":row["known_at"]})
    if exists("decision_evidence"):
        grouped = {}
        for row in db.q("SELECT * FROM decision_evidence WHERE known_at<=? ORDER BY known_at DESC,id", (at,)):
            body = db.loads(row["body"])
            scope = scopes.get(row["code"])
            missing = bool(body.get("provenance")) and scope is not None and row["id"] not in scope["ids"]
            observed = scope["known_at"] if body.get("provenance") and scope else row["known_at"]
            grouped.setdefault((row["code"],row["kind"]), []).append((not missing, observed, row, body))
        for (code, kind), candidates in grouped.items():
            active, observed, row, body = max(candidates, key=lambda x: (x[1],x[0],x[2]["id"]))
            # Expiry is evaluated only after selecting the current observation.
            # A->B->A may select A again without rewriting its first known_at.
            out.setdefault(code,{})[kind] = ({**body,"value":None,"observation_missing":True} if not active else
                body if row["expires_at"] > at else {**body,"value":None,"expired":True})
    return out


def classify(code, claims):
    yes = lambda kind: claims.get(kind, {}).get("value") is True
    negative = lambda kind: claims.get(kind, {}).get("value") is False
    common = [f"{LABELS[k]}：{v['note']}（{v['published_at']}；{v['source_url']}）"
              for k, v in claims.items() if v.get("value") is not None]
    result = {}
    for route, required in (("value", ("growth", "catalyst")), ("emotion", ("mainline",))):
        branch = "业绩催化" if route == "value" else "核心回调" if yes("core") else "提前埋伏 · 潜在核心"
        if route == "emotion":
            required += ("cooling",) if yes("core") else ("catalyst",)
        missing = [LABELS[k] for k in required if not yes(k)]
        # Failure of an entry setup (e.g. no longer cooling because price rises)
        # must never become a holding-thesis exit signal.
        thesis = ("growth", "catalyst") if route == "value" else ("mainline",)
        invalid = yes("invalid") or any(negative(k) for k in thesis)
        result[route] = {"route": route, "label": ROUTES[route], "branch": branch,
                         "core": yes("core"), "eligible": not missing and not invalid,
                         "hot_money_bonus": yes("hot_money"),
                         "hot_money_note": claims.get("hot_money", {}).get("note") or "暂无可核对龙虎榜/游资证据；仅作加分项",
                         "invalid": invalid, "missing": missing, "required_evidence": list(required), "evidence": common,
                         "evidence_ids": sorted(v["id"] for k, v in claims.items()
                                                if k in set(required) | ({"core"} if route == "emotion" and yes("core") else set())
                                                and v.get("value") is not None),
                         "state": "invalid" if invalid else "watch", "code": code}
    return result


def screen(snapshot, asof):
    evidence = latest(asof)
    out = {key: {"label": name, "candidates": [], "watch": [], "covered": 0,
                 "universe_count": len(snapshot), "asof": stamp(asof)} for key, name in ROUTES.items()}
    for quote in snapshot:
        code = quote["code"]
        if not re.fullmatch(r"(?:sh|sz)\d{6}", code):
            continue
        claims = evidence.get(code, {})
        if not claims:
            continue
        for route, result in classify(code, claims).items():
            relevant = ("growth", "catalyst") if route == "value" else ("mainline", "core", "cooling", "hot_money")
            if not any(claims.get(k, {}).get("value") is not None for k in relevant):
                continue
            out[route]["covered"] += 1
            cap = quote.get("mcap")
            cap = cap if finite(cap) and cap > 0 else None
            item = {**result, "name": quote.get("name") or code, "mcap": cap,
                    "price": quote.get("price"), "growth_metrics": claims.get("growth", {}).get("metrics", {}),
                    "stage": "观察候选；需缠论结构确认"}
            if quote.get("research_exclusion"):
                out[route]["excluded_count"] = out[route].get("excluded_count",0)+1
                continue
            if result["invalid"]:
                continue
            out[route]["candidates" if result["eligible"] else "watch"].append(item)
    for route, group in out.items():
        # Lexicographic core priority: any cap can never outrank an evidenced core.
        if route == "emotion":
            group["candidates"].sort(key=lambda x: (not x["core"], not x["hot_money_bonus"], x["mcap"] is None, x["mcap"] or math.inf, x["code"]))
        else:
            group["candidates"].sort(key=lambda x: (-(x["growth_metrics"].get("profit_yoy", 0)
                                                      - x["growth_metrics"].get("prior_profit_yoy", 0)), x["code"]))
        group["candidate_count"] = len(group["candidates"])
        group["candidates"] = group["candidates"][:10]
        group["watch"] = sorted(group["watch"], key=lambda x: x["code"])[:10]
        group["note"] = "仅使用有来源、可得时间和有效期的核对证据；缺失不补造。候选不等于参与信号。"
    return out
