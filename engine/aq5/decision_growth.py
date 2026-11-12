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
import threading
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import CN_TZ, DATA, now

VERSION = "growth-public-v2"
MAX_CODES = 10000
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


def _atomic(path, raw):
    temporary = path.with_name(path.name + ".tmp-" + str(threading.get_ident()))
    temporary.write_bytes(raw)
    temporary.replace(path)


def _capture(url, fetcher, folder, *, resume=False, cache_hours=6):
    folder.mkdir(parents=True, exist_ok=True)
    cache = folder / ("cache-" + hashlib.sha256(url.encode()).hexdigest() + ".json")
    if resume and cache.exists():
        try:
            index = json.loads(cache.read_bytes())
            source = index["source"]
            digest = source["raw_sha256"]
            age = now() - _stamp(source["retrieved_at"])
            if (index.get("validated") is True and index["url"] == url and source["rule_version"] == VERSION and re.fullmatch(r"[0-9a-f]{64}", digest)
                    and timedelta(0) <= age <= timedelta(hours=cache_hours)):
                path = folder / (digest + ".json")
                raw = path.read_bytes()
                if len(raw) <= MAX_BYTES and hashlib.sha256(raw).hexdigest() == digest:
                    return json.loads(raw), {**source, "cache_hit": True, "raw_path": str(path)}
        except (ValueError, KeyError, OSError, TypeError):
            pass  # A damaged index/raw file is never accepted as verified cache.
    raw = fetcher(url)
    if not isinstance(raw, bytes) or len(raw) > MAX_BYTES:
        raise ValueError("raw_bytes_required")
    obj = json.loads(raw)
    digest = hashlib.sha256(raw).hexdigest()
    at = now()
    path = folder / (digest + ".json")
    if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        _atomic(path, raw)
    source = {"provider": "东方财富公开转载", "raw_sha256": digest, "retrieved_at": _iso(at),
              "source_url": url, "raw_path": str(path), "rule_version": VERSION,
              "official_verified": False, "method": "public_response_current_snapshot", "cache_hit": False}
    return obj, source


def _accept_sources(sources, folder):
    # Cache only complete, scope-checked response sequences, never NO_DATA or a
    # truncated/moving page set. Failed requests are retried on the next resume.
    for source in sources:
        url = source["source_url"]
        path = folder / ("cache-" + hashlib.sha256(url.encode()).hexdigest() + ".json")
        _atomic(path, json.dumps({"url": url, "source": source, "validated": True}, ensure_ascii=False).encode())


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
    inputs = set(required) | {_quarter_before(p) for p in required if p.month != 3}
    if not inputs <= selected.keys():
        raise ValueError("missing_comparable_quarter")
    revenue_field = "TOTAL_OPERATE_INCOME"
    if any(_number(selected[p][0].get(revenue_field)) is None for p in inputs):
        financial = all(re.search(r"银行|证券|保险", str(selected[p][0].get("INDUSTRY_NAME") or "")) for p in inputs)
        alternative = all(_number(selected[p][0].get("OPERATE_INCOME")) is not None for p in inputs)
        same_basis = all(_number(selected[p][0].get("TOTAL_OPERATE_INCOME")) is None
                         or abs(selected[p][0]["TOTAL_OPERATE_INCOME"]-selected[p][0]["OPERATE_INCOME"])
                         <= max(.01, abs(selected[p][0]["TOTAL_OPERATE_INCOME"])*1e-8) for p in inputs) if alternative else False
        if not financial or not alternative or not same_basis:
            raise ValueError("missing_financial_value_or_inconsistent_revenue_basis")
        revenue_field = "OPERATE_INCOME"
    used = set()

    def standalone(period):
        if period not in selected:
            raise ValueError("missing_comparable_quarter")
        row = selected[period][0]
        used.add(period)
        values = [_number(row.get(k)) for k in (revenue_field, "PARENT_NETPROFIT")]
        if any(x is None for x in values):
            raise ValueError("missing_financial_value")
        if period.month != 3:
            prior = _quarter_before(period)
            if prior not in selected:
                raise ValueError("missing_cumulative_predecessor")
            used.add(prior)
            preceding = [_number(selected[prior][0].get(k)) for k in (revenue_field, "PARENT_NETPROFIT")]
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
                  "revenue_field": revenue_field, "revenue_basis": "营业总收入" if revenue_field == "TOTAL_OPERATE_INCOME" else "金融行业营业收入（全比较窗口同口径）",
                  "comparison_period": str(previous), "input_periods": [str(p) for p in sorted(used)],
                  "raw_values": [{"period": str(p), "revenue_ytd": selected[p][0][revenue_field],
                                  "profit_ytd": selected[p][0]["PARENT_NETPROFIT"],
                                  "notice_date": selected[p][0]["NOTICE_DATE"]} for p in sorted(used)]}
    return {"code": code, "kind": "growth", "value": positive, "note": note,
            "source_url": f"https://data.eastmoney.com/bbsj/lrb/{code[-6:]}.html", "published_at": _iso(published), "available_at": _iso(available),
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


def _paged_source(sources):
    result = dict(sources[0])
    result.update(retrieved_at=max(s["retrieved_at"] for s in sources),
                  raw_sha256s=[s["raw_sha256"] for s in sources],
                  page_sources=[{k: s[k] for k in ("source_url", "raw_sha256", "retrieved_at", "cache_hit")} for s in sources],
                  cache_hit=all(s["cache_hit"] for s in sources))
    return result


def collect(codes, asof=None, *, fetcher=None, storage_dir=None, resume=True, cache_hours=6,
            max_workers=3, progress=None):
    """Evaluate every supplied code with disk-resumable, bounded concurrency.

    A cached response retains its original retrieved_at. NO_DATA and negative YoY
    bases have different statuses. progress receives compact stage counters.
    """
    codes = list(dict.fromkeys(str(c).lower() for c in codes))
    if not codes or len(codes) > MAX_CODES or any(not re.fullmatch(r"(?:sh|sz)\d{6}", c) for c in codes):
        raise ValueError("require_1_to_10000_explicit_stock_codes")
    if type(max_workers) is not int or not 1 <= max_workers <= 3 or not 0 <= cache_hours <= 24:
        raise ValueError("invalid_collection_budget")
    fetcher = fetcher or _request
    folder = Path(storage_dir) if storage_dir else DATA / "decision-sources" / "growth"
    folder.mkdir(parents=True, exist_ok=True)
    outcomes = folder / "outcomes"
    outcomes.mkdir(exist_ok=True)
    requested = _stamp(asof) if asof is not None else None
    floor = date((requested or now()).year-2, 1, 1).isoformat()

    def captured(url):
        return _capture(url, fetcher, folder, resume=resume, cache_hours=cache_hours)

    def advance(stage, completed):
        receipt = {"version": VERSION, "stage": stage, "completed": completed, "requested": len(codes),
                   "universe_sha256": hashlib.sha256(json.dumps(sorted(codes)).encode()).hexdigest(), "updated_at": _iso(now())}
        _atomic(folder / "progress.json", json.dumps(receipt).encode())
        if progress is not None:
            progress(receipt)

    def financial_batch(batch):
        selected = ",".join('"' + code[-6:] + '"' for code in batch)
        params = {"reportName": "RPT_DMSK_FN_INCOME",
            "columns": "SECURITY_CODE,SECURITY_NAME_ABBR,INDUSTRY_NAME,REPORT_DATE,NOTICE_DATE,TOTAL_OPERATE_INCOME,OPERATE_INCOME,PARENT_NETPROFIT",
            "filter": f"(SECURITY_CODE in ({selected}))(REPORT_DATE>='{floor}')", "pageSize": "500",
            "pageNumber": "1", "sortColumns": "REPORT_DATE,SECURITY_CODE", "sortTypes": "-1,1"}
        rows, sources, expected, pages = [], [], None, None
        try:
            for page in range(1, 11):
                obj, source = captured(FIN_BASE + "?" + urllib.parse.urlencode({**params, "pageNumber": str(page)}))
                sources.append(source)
                if not isinstance(obj, dict):
                    raise ValueError("financial_response_schema")
                result = obj.get("result") or {}
                if not isinstance(result, dict):
                    raise ValueError("financial_response_schema")
                current = result.get("data")
                if obj.get("success") is not True or not isinstance(current, list) or not current:
                    raise ValueError("financial_no_data" if not current else "financial_response_schema")
                if any(not isinstance(r, dict) for r in current):
                    raise ValueError("financial_response_schema")
                count, total_pages = result.get("count"), result.get("pages")
                if type(count) is not int or type(total_pages) is not int or count < 1 or not 1 <= total_pages <= 10:
                    raise ValueError("financial_pagination_metadata")
                if expected is not None and (count != expected or total_pages != pages):
                    raise ValueError("financial_pagination_changed")
                expected, pages = count, total_pages
                if any(str(r.get("SECURITY_CODE")) not in {c[-6:] for c in batch} for r in current):
                    raise ValueError("financial_scope_mismatch")
                rows.extend(current)
                if page == pages:
                    break
            keys = [(r.get("SECURITY_CODE"), r.get("REPORT_DATE")) for r in rows]
            if len(rows) != expected or len(set(keys)) != len(keys):
                raise ValueError("financial_batch_incomplete")
            _accept_sources(sources, folder)
            return batch, rows, _paged_source(sources), None, sources
        except (ValueError, KeyError, TypeError, OSError, RuntimeError) as exc:
            return batch, [], None, str(exc)[:160], sources

    financial, sources = {}, []
    chunks = [codes[i:i+20] for i in range(0, len(codes), 20)]
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for batch, rows, source, error, pages in pool.map(financial_batch, chunks):
            sources.extend(pages)
            for code in batch:
                financial[code] = (rows, source, error)
            advance("financial", len(financial))

    def announcements(code, cutoff):
        params = {"page_size": "100", "ann_type": "A", "client_source": "web", "stock_list": code[-6:],
                  "begin_time": (cutoff-timedelta(days=30)).date().isoformat(), "end_time": cutoff.date().isoformat()}
        rows, sources, total = [], [], None
        try:
            for page in range(1, 21):
                obj, source = captured(ANN_BASE + "?" + urllib.parse.urlencode({**params, "page_index": str(page)}))
                sources.append(source)
                if not isinstance(obj, dict):
                    raise ValueError("announcement_response_schema")
                meta = obj.get("data") or {}
                if not isinstance(meta, dict):
                    raise ValueError("announcement_response_schema")
                current = meta.get("list")
                if obj.get("success") not in (True, 1) or not isinstance(current, list) or not current:
                    raise ValueError("announcement_no_data")
                if any(not isinstance(r, dict) or not isinstance(r.get("codes"), list)
                       or any(not isinstance(c, dict) for c in r["codes"]) for r in current):
                    raise ValueError("announcement_response_schema")
                count = meta.get("total_hits")
                if type(count) is not int or not 0 < count <= 2000 or meta.get("page_index") != page:
                    raise ValueError("announcement_pagination_metadata")
                if total is not None and total != count:
                    raise ValueError("announcement_pagination_changed")
                total = count
                if any(code[-6:] not in {str(c.get("stock_code")) for c in r.get("codes", [])} for r in current):
                    raise ValueError("announcement_scope_mismatch")
                rows.extend(current)
                if len(rows) >= total:
                    break
            ids = [r.get("art_code") for r in rows]
            if len(rows) != total or len(set(ids)) != len(ids):
                raise ValueError("announcement_window_incomplete")
            _accept_sources(sources, folder)
            return rows, _paged_source(sources), sources, None
        except (ValueError, KeyError, TypeError, OSError, RuntimeError) as exc:
            return [], None, sources, str(exc)[:160]

    def one(code):
        records, failures, sources, events = [], [], [], []
        status = {"code": code, "financial": {"attempted": True, "fetched": False, "status": "missing"},
                  "catalyst": {"attempted": False, "status": "not_evaluated", "reason": "financial_unqualified"}}
        try:
            rows, source, error = financial[code]
            if error:
                raise ValueError(error)
            status["financial"].update(fetched=any(str(r.get("SECURITY_CODE")) == code[-6:] for r in rows),
                                       cache_hit=source["cache_hit"], source_observed_at=source["retrieved_at"])
            growth = growth_record(code, rows, source, requested or now())
            records.append(growth)
            status["financial"].update(status="qualified", value=growth["value"], reason="comparable_quarters",
                                       revenue_field=growth["provenance"]["revenue_field"])
        except (ValueError, KeyError, TypeError, OSError, RuntimeError) as exc:
            reason = str(exc)[:160]
            state = "nonapplicable" if reason == "nonpositive_yoy_base" else "missing"
            status["financial"].update(status=state, reason=reason)
            if state == "missing":
                failures.append({"code": code, "stage": "growth", "reason": reason})
            return records, failures, sources, events, status
        if not growth["value"]:
            status["catalyst"].update(status="not_required", reason="growth_not_accelerating")
            return records, failures, sources, events, status
        status["catalyst"].update(attempted=True, status="missing")
        try:
            items, source, pages, error = announcements(code, requested or now())
            sources.extend(pages)
            if error:
                raise ValueError(error)
            catalyst, events = catalyst_record(code, growth, items, source, requested or now())
            if catalyst:
                records.append(catalyst)
            status["catalyst"].update(status="qualified", value=catalyst is not None,
                                     reason="matching_release" if catalyst else "no_matching_recent_release",
                                     cache_hit=source["cache_hit"], source_observed_at=source["retrieved_at"])
        except (ValueError, KeyError, TypeError, OSError, RuntimeError) as exc:
            status["catalyst"]["reason"] = str(exc)[:160]
            failures.append({"code": code, "stage": "catalyst", "reason": str(exc)[:160]})
        return records, failures, sources, events, status

    records, failures, events, statuses = [], [], [], {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for rs, fs, ss, es, status in pool.map(one, codes):
            records.extend(rs); failures.extend(fs); sources.extend(ss); events.extend(es)
            statuses[status["code"]] = status
            _atomic(outcomes / (status["code"] + ".json"), json.dumps({"version": VERSION, "status": status,
                    "records": rs, "failures": fs, "updated_at": _iso(now())}, ensure_ascii=False).encode())
            if len(statuses) % 20 == 0 or len(statuses) == len(codes):
                advance("evidence", len(statuses))
    tally = lambda section, value: sum(s[section]["status"] == value for s in statuses.values())
    return {"version": VERSION, "records": records, "events": events, "failures": failures, "sources": sources,
            "status_by_code": statuses,
            "coverage": {"requested": len(codes), "codes": codes, "growth": sum(r["kind"] == "growth" for r in records),
                         "accelerating": sum(r["kind"] == "growth" and r["value"] for r in records),
                         "catalyst": sum(r["kind"] == "catalyst" for r in records), "full_market": False,
                         "attempted": len(statuses), "all_requested_attempted": len(statuses) == len(codes),
                         "financial_fetched": sum(s["financial"]["fetched"] for s in statuses.values()),
                         "qualified": tally("financial", "qualified"), "nonapplicable": tally("financial", "nonapplicable"),
                         "missing": tally("financial", "missing"), "catalyst_required": sum(s["catalyst"]["attempted"] for s in statuses.values()),
                         "catalyst_qualified": tally("catalyst", "qualified"), "catalyst_missing": tally("catalyst", "missing"),
                         "catalyst_not_required": tally("catalyst", "not_required"),
                         "source_responses": len(sources), "cache_hits": sum(s["cache_hit"] for s in sources)},
            "note": "逐股报告指定集合的已尝试/合格/非适用/缺失，不将NO_DATA或非正同比基数当作不满足。"
                    "缓存保留原抓取时间；公开转载修订快照不倒填历史，标题关键词不推断正面催化。"}
