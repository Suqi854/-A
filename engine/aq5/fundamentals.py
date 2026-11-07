"""公告财务快照。仅对导入且在信号时点可见的数据做质量比较。"""
from __future__ import annotations

import math
import re
from datetime import date, datetime, timedelta
from urllib.parse import parse_qsl, urlsplit

from . import db, market
from .config import CN_TZ

HOSTS = {"cninfo.com.cn", "sse.com.cn", "szse.cn"}
FIELDS = ("roe", "debt_ratio", "revenue_ttm", "profit_ttm", "operating_cashflow_ttm")
REQUIRED = {*FIELDS, "code", "period_end", "published_at", "source_url", "basis"}
PUBLIC_QUERY = {
    "announcementId": r"\d{1,20}", "stockCode": r"\d{6}",
    "orgId": r"(?:gssz|gssh|9900)\d{4,20}", "announcementTime": r"\d{4}-\d{2}-\d{2}",
}


def init() -> None:
    with db.tx() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS research_disclosures(
            code TEXT, period_end TEXT, published_at TEXT, available_at TEXT, source TEXT, values_json TEXT,
            PRIMARY KEY(code, period_end, published_at))""")


def stamp(value: str) -> str:
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError("公告可用时间必须包含时区")
    return parsed.astimezone(CN_TZ).isoformat(timespec="seconds")


def import_disclosures(records: list[dict]) -> dict:
    """导入已人工核对的公告数据。ROE 和负债率用小数，流量字段为人民币 TTM。"""
    if not isinstance(records, list) or not records:
        raise ValueError("需要非空公告记录列表")
    rows = []
    for r in records:
        if not isinstance(r, dict) or not REQUIRED <= r.keys():
            raise ValueError("缺少代码、报告期、公告时间、来源或必需的财务字段")
        code = market.norm_code(str(r["code"]))
        if not re.fullmatch(r"(sh|sz)\d{6}", code) or not market.is_main_board(code):
            raise ValueError("仅支持沪深主板代码")
        period = date.fromisoformat(str(r["period_end"])).isoformat()
        raw_published = str(r["published_at"])
        if len(raw_published) == 10:
            disclosed_day = date.fromisoformat(raw_published)
            published = disclosed_day.isoformat()
            # 只有官方公告日期时，保守地从次日零点起使用，避免把盘后公告算入当天信号。
            available = (disclosed_day + timedelta(days=1)).isoformat() + "T00:00:00+08:00"
        else:
            published = available = stamp(raw_published)
        if period > published[:10] or published > datetime.now(CN_TZ).isoformat(timespec="seconds"):
            raise ValueError("报告期或公告时间超出可用时点")
        source = str(r["source_url"])
        url = urlsplit(source)
        host = url.hostname or ""
        if (url.scheme != "https" or url.username or url.password
                or not any(host == h or host.endswith("." + h) for h in HOSTS)
                or url.path in ("", "/") or url.fragment
                or any(k not in PUBLIC_QUERY or not re.fullmatch(PUBLIC_QUERY[k], v)
                       for k, v in parse_qsl(url.query, keep_blank_values=True))):
            raise ValueError("需要交易所或巨潮资讯的公开公告链接")
        if r.get("basis") != "ttm_cny":
            raise ValueError("财务口径必须是人民币 TTM")
        if any(type(r.get(k)) not in (int, float) for k in FIELDS):
            raise ValueError("财务字段需要完整数值")
        values = {k: float(r[k]) for k in FIELDS}
        if (not all(math.isfinite(v) for v in values.values()) or values["revenue_ttm"] <= 0
                or values["debt_ratio"] < 0):
            raise ValueError("财务字段数值无效")
        rows.append((code, period, published, available, source, db.dumps(values)))
    init()
    inserted = 0
    with db.tx() as c:
        for code, period, published, available, source, values in rows:
            prev = c.execute("""SELECT source, values_json FROM research_disclosures
                WHERE code=? AND period_end=? AND published_at=?""", (code, period, published)).fetchone()
            if prev:
                if prev["source"] != source or db.loads(prev["values_json"], {}) != db.loads(values, {}):
                    raise ValueError("同一时点公告记录冲突，修订须提供独立发布时间")
                continue
            c.execute("INSERT INTO research_disclosures VALUES(?,?,?,?,?,?)",
                      (code, period, published, available, source, values))
            inserted += 1
    return {"inserted": inserted, "received": len(records), "source_verified_online": False}


def screen(snapshot: list[dict], asof: str, limit: int = 10) -> tuple[dict, dict]:
    """只使用公告可用时间不晚于 asof 的最新一期。缺覆盖和缺 PE 都显式保留。"""
    asof = stamp(asof)
    quotes = {r["code"]: r for r in snapshot}
    current = {}
    if db.q1("SELECT 1 FROM sqlite_master WHERE type='table' AND name='research_disclosures'"):
        rows = db.q("""SELECT * FROM research_disclosures WHERE available_at<=?
                       ORDER BY period_end DESC, published_at DESC""", (asof,))
        for r in rows:
            if r["code"] in quotes:
                current.setdefault(r["code"], r)
    ranked, features = [], {}
    priced = 0
    for code, row in current.items():
        f = db.loads(row["values_json"], {})
        q = quotes[code]
        pe = q.get("pe")
        has_pe = isinstance(pe, (int, float)) and math.isfinite(pe)
        priced += int(has_pe)
        if f["profit_ttm"] <= 0 or f["operating_cashflow_ttm"] <= 0 or f["roe"] <= 0:
            features[code] = 0.0
            continue
        cash_conversion = f["operating_cashflow_ttm"] / f["profit_ttm"]
        quality = round(100 * (0.45 * min(f["roe"] / 0.25, 1)
                              + 0.35 * min(cash_conversion / 1.5, 1)
                              + 0.20 * max(0, 1 - f["debt_ratio"])), 2)
        features[code] = quality
        if not has_pe or pe <= 0:
            continue
        ranked.append({"code": code, "name": q.get("name"), "pe": pe, "quality": quality,
                       "value_quality_score": round(0.7 * quality + 30 * min(12 / pe, 1), 2),
                       "period_end": row["period_end"], "published_at": row["published_at"], "available_at": row["available_at"],
                       "source_url": row["source"], **f})
    ranked.sort(key=lambda r: (-r["value_quality_score"], r["code"]))
    status = "unavailable" if not current else "partial"
    if quotes and priced == len(quotes):
        status = "snapshot_covered"
    return {"status": status, "asof": asof, "universe_count": len(quotes),
            "disclosure_count": len(current), "valuation_count": priced, "candidate_count": len(ranked),
            "candidates": ranked[:limit], "full_industry_screen": False,
            "data_note": "仅比较本次快照内有合格公告和正市盈率的股票。公告链接及数值由导入者核对。"
                         if current else "缺少可用时点的官方公告导入，无法评估价值和经营质量。"}, features
