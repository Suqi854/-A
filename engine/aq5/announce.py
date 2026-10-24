"""公告监控：只盯自选 + 持仓 + 近 7 天选股池，新公告写入事件（kind=announce）供小喇叭提醒。

数据源：东方财富公告接口 np-anotice-stock（display_time 精确到毫秒，能区分午间发布）。
巨潮只给日期（00:00:00），不能判断是否午间发布，所以不用。
只做"某公告发出时 / 午后开盘时该股表现"的时间关联提示，不判断因果。
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta

from . import db, market, notify
from .config import MOCK, load_settings, now, today

URL = ("https://np-anotice-stock.eastmoney.com/api/security/ann?page_size=100&page_index={p}"
       "&ann_type=A&client_source=web&f_node=0&s_node=0")
MAX_PAGES = 4
STATUS = {"last": None, "ok": None, "error": None, "new": 0}

# 例行公告，不提醒
SKIP = re.compile(r"投资者关系|律师|法律意见|核查意见|公司章程|议事规则|管理制度|独立董事|摘要|更正|补充")
# 值得留意的关键词 -> 标签；命中则 level=warn
KEY = [("重组", "重大重组"), ("停牌", "停牌"), ("复牌", "复牌"), ("立案", "立案/处罚"), ("处罚", "立案/处罚"), ("退市", "退市风险"),
       ("风险提示", "风险提示"), ("预增", "业绩预增"), ("扭亏", "业绩预增"), ("预减", "业绩预减"), ("预亏", "业绩预亏"),
       ("中标", "中标/合同"), ("合同", "中标/合同"), ("回购", "回购"), ("增持", "增持"), ("减持", "减持"), ("收购", "收购"),
       ("非公开发行", "定增"), ("向特定对象发行", "定增"), ("股权激励", "股权激励"), ("问询函", "问询函"), ("诉讼", "诉讼仲裁"),
       ("重大事项", "重大事项"), ("澄清", "澄清公告")]

SCHEMA = """
CREATE TABLE IF NOT EXISTS ann_seen(key TEXT PRIMARY KEY, code TEXT, name TEXT, title TEXT, ts TEXT, tag TEXT);
"""


def init() -> None:
    with db.tx() as c:
        c.executescript(SCHEMA)


def watch_codes() -> dict[str, str]:
    """6 位代码 -> 来源（自选/持仓/选股）。"""
    out: dict[str, str] = {}
    for r in db.q("SELECT DISTINCT code FROM picks WHERE date >= date(?, '-7 day')", (today(),)):
        out[r["code"][-6:]] = "选股"
    for r in db.q("SELECT DISTINCT code FROM positions"):
        out[r["code"][-6:]] = "持仓"
    for c in load_settings().get("watchlist", []):
        out[market.norm_code(c)[-6:]] = "自选"
    return out


def _fetch() -> list[dict]:
    items: list[dict] = []
    for p in range(1, MAX_PAGES + 1):
        d = json.loads(market.http_get(URL.format(p=p), referer="https://data.eastmoney.com/notices/", timeout=10))
        L = (d.get("data") or {}).get("list") or []
        items += L
        if len(L) < 100:
            break
    return items


def _ts(a: dict) -> str:
    s = str(a.get("display_time") or a.get("notice_date") or "")
    return s[:19]


def classify(title: str) -> tuple[bool, str]:
    """返回（是否提醒, 标签）。"""
    if SKIP.search(title):
        return False, ""
    for k, tag in KEY:
        if k in title:
            return True, tag
    return True, ""


def poll() -> int:
    """拉一轮公告，新的写事件，返回新增条数。首次运行只静默入库，两小时内的才提醒。"""
    if MOCK:
        return 0
    try:
        items = _fetch()
    except Exception as e:  # noqa: BLE001
        STATUS.update(last=now().strftime("%H:%M:%S"), ok=False, error=str(e)[:120])
        return 0
    watch = watch_codes()
    first = db.q1("SELECT COUNT(*) n FROM ann_seen")["n"] == 0
    cutoff = (now() - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    fresh: list[tuple] = []
    for a in items:
        for cd in a.get("codes") or []:
            code = str(cd.get("stock_code") or "")
            if code not in watch:
                continue
            title = a.get("title") or ""
            key = a.get("art_code") or f"{code}|{title}|{a.get('display_time')}"
            if db.q1("SELECT 1 FROM ann_seen WHERE key=?", (f"{key}|{code}",)):
                continue
            ts = _ts(a)
            ok, tag = classify(title)
            with db.tx() as c:
                c.execute("INSERT OR IGNORE INTO ann_seen(key, code, name, title, ts, tag) VALUES(?,?,?,?,?,?)",
                          (f"{key}|{code}", code, cd.get("short_name") or code, title, ts, tag if ok else "-"))
            if ok and (not first or ts >= cutoff):
                fresh.append((code, cd.get("short_name") or code, title, ts, tag, watch[code]))
    STATUS.update(last=now().strftime("%H:%M:%S"), ok=True, error=None, new=len(fresh))
    if not fresh:
        return 0
    try:
        qs = market.quotes(list({c for c, *_ in fresh}))
    except Exception:  # noqa: BLE001
        qs = {}
    for code, name, title, ts, tag, src in fresh:
        q = qs.get(market.norm_code(code)) or {}
        px = f"，发布后现涨幅 {q['pct']:+.2f}%" if q.get("pct") is not None else ""
        db.event("announce", f"{name}（{src}）{('【' + tag + '】') if tag else ''}{title[:50]}", f"发布 {ts[5:16]}{px}",
                 "warn" if tag else "info")
        if tag:
            notify.send(f"公告 {name}", f"{title}\n发布 {ts}{px}")
    return len(fresh)


def noon_followup() -> None:
    """午后开盘：列出午间（11:30-13:00）发布过公告的盯盘股此刻表现，只做时间关联提示。"""
    rows = db.q("SELECT * FROM ann_seen WHERE ts >= ? AND ts < ? AND tag != '-'",
                (today() + " 11:30:00", today() + " 13:00:00"))
    if not rows:
        return
    qs = market.quotes(list({r["code"] for r in rows}))
    lines = []
    for r in rows:
        q = qs.get(market.norm_code(r["code"])) or {}
        if q.get("pct") is not None:
            lines.append(f"{r['name']} 现 {q['pct']:+.2f}%：{r['title'][:30]}（{r['ts'][11:16]}）")
    if lines:
        db.event("announce", f"午间公告股午后表现（{len(lines)} 只）", "\n".join(lines[:10]) + "\n仅时间关联，非因果判断", "info")
