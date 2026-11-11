"""Current public financial snapshots and dated earnings-release events.

Eastmoney is a public redistributor, not independent official verification. The
collector never backfills historical knowledge or treats headline keywords as a
positive catalyst. Cumulative income statements are differenced into quarters.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import CN_TZ, DATA, now

VERSION = "growth-public-v1"
MAX_CODES = 100
MAX_BYTES = 4 * 1024 * 1024
FIN_BASE = "https://datacenter-web.eastmoney.com/api/data/v1/get"
ANN_BASE = "https://np-anotice-stock.eastmoney.com/api/security/ann"


def _stamp(value):
    result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if result.tzinfo is None:
        raise ValueError("timezone_required")
    return result.astimezone(CN_TZ)


def _iso(value):
    return _stamp(value).isoformat(timespec="seconds")


def _number(value):
    return float(value) if type(value) in (int, float) and math.isfinite(value) else None


def _request(url):
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"})
    with urllib.request.urlopen(request, timeout=12) as response:
        raw = response.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("source_response_too_large")
    return raw


def _capture(url, fetcher, folder):
    raw = fetcher(url)
    if not isinstance(raw, bytes) or len(raw) > MAX_BYTES:
        raise ValueError("raw_bytes_required")
    obj = json.loads(raw)
    digest = hashlib.sha256(raw).hexdigest()
    at = now()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (digest + ".json")
    if not path.exists():
        path.write_bytes(raw)
    source = {"provider": "东方财富公开转载", "raw_sha256": digest, "retrieved_at": _iso(at),
              "source_url": url, "raw_path": str(path), "rule_version": VERSION,
              "official_verified": False, "method": "public_response_current_snapshot"}
    return obj, source


def _quarter_before(period):
    year, month = period.year, period.month
    return date(year-1, 12, 31) if month == 3 else date(year, month-3, 31 if month == 6 else 30)


def _report_day(value):
    parsed = date.fromisoformat(str(value)[:10])
    if (parsed.month, parsed.day) not in ((3, 31), (6, 30), (9, 30), (12, 31)):
        raise ValueError("not_quarter_end")
    return parsed


def _publication(value):
    # Financial API supplies date-only NOTICE_DATE (usually 00:00). Conservatively
    # available next day; retain publication separately from actual acquisition.
    day = date.fromisoformat(str(value)[:10])
    published = datetime.combine(day, datetime.min.time(), CN_TZ)
    return published, published + timedelta(days=1)


def growth_record(code, rows, source, cutoff):
    cutoff = _stamp(cutoff)
    received = _stamp(source["retrieved_at"])
    if received > cutoff:
        raise ValueError("snapshot_not_known_at_cutoff")
    selected = {}
    for row in rows:
        if str(row.get("SECURITY_CODE")) != code[-6:]:
            continue
        period = _report_day(row.get("REPORT_DATE"))
        published, available = _publication(row.get("NOTICE_DATE"))
        if available > cutoff or period > published.date():
            continue
        if period in selected and row != selected[period][0]:
            raise ValueError("conflicting_report_revisions")
        selected[period] = (row, published, available)
    if not selected:
        raise ValueError("no_available_financial_reports")
    current = max(selected)
    if (cutoff.date() - current).days > 180:
        raise ValueError("latest_financial_period_stale")
    previous = _quarter_before(current)
    required = [current, previous, current.replace(year=current.year-1), previous.replace(year=previous.year-1)]
    used = set()

    def standalone(period):
        if period not in selected:
            raise ValueError("missing_comparable_quarter")
        row = selected[period][0]
        used.add(period)
        values = [_number(row.get(k)) for k in ("TOTAL_OPERATE_INCOME", "PARENT_NETPROFIT")]
        if any(x is None for x in values):
            raise ValueError("missing_financial_value")
        if period.month != 3:
            prior = _quarter_before(period)
            if prior not in selected:
                raise ValueError("missing_cumulative_predecessor")
            used.add(prior)
            preceding = [_number(selected[prior][0].get(k)) for k in ("TOTAL_OPERATE_INCOME", "PARENT_NETPROFIT")]
            if any(x is None for x in preceding):
                raise ValueError("missing_financial_value")
            values = [a-b for a, b in zip(values, preceding)]
        return values

    cur, prior, cur_yearago, prior_yearago = [standalone(p) for p in required]
    if any(x <= 0 for x in cur_yearago + prior_yearago):
        raise ValueError("nonpositive_yoy_base")
    revenue, profit = [(a/b-1)*100 for a, b in zip(cur, cur_yearago)]
    prior_revenue, prior_profit = [(a/b-1)*100 for a, b in zip(prior, prior_yearago)]
    metrics = {"revenue_yoy": revenue, "prior_revenue_yoy": prior_revenue,
               "profit_yoy": profit, "prior_profit_yoy": prior_profit}
    positive = revenue > max(0, prior_revenue) and profit > max(0, prior_profit)
    published = selected[current][1]
    available = max([received] + [selected[p][2] for p in used])
    expiry = max(selected[p][1] for p in used) + timedelta(days=120)
    if expiry <= available:
        raise ValueError("financial_disclosure_expired")
    note = (f"{current}对比{previous}，由累计报表差分的单季营收同比{revenue:.2f}%/{prior_revenue:.2f}%，"
            f"归母净利同比{profit:.2f}%/{prior_profit:.2f}%；{'两项均正增长并加速' if positive else '未同时满足正增长加速'}。"
            "东方财富现时修订快照，非历史时点财报；未独立核验原公告数值。")
    provenance = {**source, "method": "ytd_difference_then_same_quarter_yoy", "period_end": str(current),
                  "comparison_period": str(previous), "input_periods": [str(p) for p in sorted(used)],
                  "raw_values": [{"period": str(p), "revenue_ytd": selected[p][0]["TOTAL_OPERATE_INCOME"],
                                  "profit_ytd": selected[p][0]["PARENT_NETPROFIT"],
                                  "notice_date": selected[p][0]["NOTICE_DATE"]} for p in sorted(used)]}
    return {"code": code, "kind": "growth", "value": positive, "note": note,
            "source_url": f"https://data.eastmoney.com/bbsj/{code[-6:]}_lrb.html", "published_at": _iso(published), "available_at": _iso(available),
            "expires_at": _iso(expiry), "basis": "comparable_quarter_yoy",
            "metrics": metrics, "provenance": provenance}


def catalyst_record(code, growth, announcements, source, cutoff):
    """A recent, identified earnings release is a catalyst event only when its
    financial acceleration is quantified. Other title hits remain observations.
    """
    cutoff = _stamp(cutoff)
    period = date.fromisoformat(growth["provenance"]["period_end"])
    term = {3: r"第?[一1]季度报告", 6: "半年度报告", 9: r"第?[三3]季度报告", 12: "年度报告"}[period.month]
    matching, events = [], []
    for item in announcements:
        if code[-6:] not in {str(x.get("stock_code")) for x in item.get("codes", [])}:
            continue
        title, art = str(item.get("title") or ""), str(item.get("art_code") or "")
        if not re.fullmatch(r"AN\d+", art):
            continue
        rawtime = str(item.get("display_time") or "")[:19]
        try:
            published = datetime.fromisoformat(rawtime).replace(tzinfo=CN_TZ)
        except ValueError:
            continue
        if published > cutoff or published < cutoff-timedelta(days=30):
            continue
        url = f"https://data.eastmoney.com/notices/detail/{code[-6:]}/{art}.html"
        event = {"code": code, "title": title, "published_at": _iso(published), "source_url": url,
                 "direction": "unverified", "not_positive_catalyst": True}
        if re.search(r"中标|合同|回购|重组|增持|减持|报告|预告|诉讼|风险", title):
            events.append(event)
        if (re.search(str(period.year) + r"年\s*" + term + r"(?:全文)?$", title)
                and not re.search(r"取消|更正|修订|摘要|提示|说明会", title)):
            matching.append((published, event))
    if not growth["value"] or not matching:
        return None, events[:10]
    published, event = max(matching, key=lambda pair: pair[0])
    financial_pub = _stamp(growth["published_at"])
    if abs((financial_pub.date()-published.date()).days) > 2:
        return None, events[:10]
    expiry = published + timedelta(days=30)
    available = max(_stamp(growth["available_at"]), _stamp(source["retrieved_at"]), published)
    if available > cutoff or expiry <= available:
        return None, events[:10]
    record = {"code": code, "kind": "catalyst", "value": True,
              "note": f"业绩披露事件：{event['title']}，数值满足单季营收及归母净利同比加速，处于发布后30日观察窗；不证明股价催化效果。",
              "source_url": event["source_url"], "published_at": _iso(published), "available_at": _iso(available),
              "expires_at": _iso(expiry), "provenance": {**source, "method": "dated_earnings_release_and_quantified_acceleration",
              "financial_raw_sha256": growth["provenance"]["raw_sha256"], "period_end": str(period),
              "event_title": event["title"], "catalyst_effect_verified": False}}
    return record, events[:10]


def collect(codes, asof=None, *, fetcher=None, storage_dir=None):
    """Collect a bounded explicit universe; no database or account mutations."""
    codes = list(dict.fromkeys(str(c).lower() for c in codes))
    if not codes or len(codes) > MAX_CODES or any(not re.fullmatch(r"(?:sh|sz)\d{6}", c) for c in codes):
        raise ValueError("require_1_to_100_explicit_stock_codes")
    fetcher = fetcher or _request
    folder = Path(storage_dir) if storage_dir else DATA / "decision-sources" / "growth"
    requested = _stamp(asof) if asof is not None else None
    floor = date((requested or now()).year-2, 1, 1).isoformat()

    def financial_batch(batch):
        selected = ",".join('"' + code[-6:] + '"' for code in batch)
        url = FIN_BASE + "?" + urllib.parse.urlencode({"reportName": "RPT_DMSK_FN_INCOME",
            "columns": "SECURITY_CODE,REPORT_DATE,NOTICE_DATE,TOTAL_OPERATE_INCOME,PARENT_NETPROFIT",
            "filter": f"(SECURITY_CODE in ({selected}))(REPORT_DATE>='{floor}')", "pageSize": "500",
            "pageNumber": "1", "sortColumns": "REPORT_DATE,SECURITY_CODE", "sortTypes": "-1,1"})
        try:
            obj, source = _capture(url, fetcher, folder)
            result = obj.get("result") or {}
            rows = result.get("data")
            if obj.get("success") is not True or not isinstance(rows, list):
                raise ValueError("financial_response_schema")
            if result.get("pages") != 1 or result.get("count") != len(rows):
                raise ValueError("financial_batch_incomplete")
            return batch, rows, source, None
        except (ValueError, KeyError, TypeError, OSError, RuntimeError) as exc:
            return batch, [], None, str(exc)[:160]

    financial, sources = {}, []
    chunks = [codes[i:i+20] for i in range(0, len(codes), 20)]
    with ThreadPoolExecutor(max_workers=3) as pool:
        for batch, rows, source, error in pool.map(financial_batch, chunks):
            if source:
                sources.append(source)
            for code in batch:
                financial[code] = (rows, source, error)

    def one(code):
        records, failures, sources, events = [], [], [], []
        try:
            rows, source, error = financial[code]
            if error:
                raise ValueError(error)
            growth = growth_record(code, rows, source, requested or now())
            records.append(growth)
        except (ValueError, KeyError, TypeError, OSError, RuntimeError) as exc:
            failures.append({"code": code, "stage": "growth", "reason": str(exc)[:160]})
            return records, failures, sources, events
        if not growth["value"]:
            return records, failures, sources, events
        ann_url = ANN_BASE + "?" + urllib.parse.urlencode({"page_size": "100", "page_index": "1", "ann_type": "A",
                                                         "client_source": "web", "stock_list": code[-6:]})
        try:
            obj, source = _capture(ann_url, fetcher, folder)
            sources.append(source)
            items = (obj.get("data") or {}).get("list")
            if not isinstance(items, list):
                raise ValueError("announcement_response_schema")
            catalyst, events = catalyst_record(code, growth, items, source, requested or now())
            if catalyst:
                records.append(catalyst)
        except (ValueError, KeyError, TypeError, OSError, RuntimeError) as exc:
            failures.append({"code": code, "stage": "catalyst", "reason": str(exc)[:160]})
        return records, failures, sources, events

    records, failures, events = [], [], []
    with ThreadPoolExecutor(max_workers=3) as pool:
        for rs, fs, ss, es in pool.map(one, codes):
            records.extend(rs); failures.extend(fs); sources.extend(ss); events.extend(es)
    return {"version": VERSION, "records": records, "events": events, "failures": failures, "sources": sources,
            "coverage": {"requested": len(codes), "codes": codes, "growth": sum(r["kind"] == "growth" for r in records),
                         "accelerating": sum(r["kind"] == "growth" and r["value"] for r in records),
                         "catalyst": sum(r["kind"] == "catalyst" for r in records), "full_market": False},
            "note": "仅指定股票，公开转载数据。标题关键词不推断正面催化；修订快照按本次可得时间使用。"}
