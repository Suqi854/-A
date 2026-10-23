"""板块 / 行业维度：行业强度、概念热度、个股所属行业。

数据：新浪行业板块（约 49 个行业）和概念板块；个股→行业的对应关系每周用新浪成分股刷新一次，存在本地。
行业强度（0~100）由我们自己的全市场快照算出，按排名百分位合成：
  今日平均涨幅 40% + 上涨家数比例 20% + 涨停家数 15% + 近5日累计涨幅 25%（历史不足 3 天时这 25% 分给前两项）
个股的「板块」因子 = 所属行业的强度；未知行业按 50 分（中性）处理。
"""
from __future__ import annotations

import json
import re
import threading
import time

from . import db, market
from .config import now, today

SCHEMA = """
CREATE TABLE IF NOT EXISTS stock_sector(code TEXT PRIMARY KEY, industry TEXT, ind_code TEXT, updated TEXT);
CREATE TABLE IF NOT EXISTS sector_daily(date TEXT, ind_code TEXT, name TEXT, pct REAL, up_ratio REAL, limit_up INTEGER,
  amount REAL, score REAL, PRIMARY KEY(date, ind_code));
"""
SINA = "https://vip.stock.finance.sina.com.cn"
STATE = {"map_running": False, "map_updated": None, "map_count": 0, "error": None}
_lock = threading.Lock()
_mem = {"t": 0.0, "data": None}
MOCK_INDUSTRIES = ["银行", "白酒", "电力", "半导体", "汽车制造", "医药", "化工", "有色金属", "房地产", "电子器件",
                   "传媒娱乐", "机械行业"]


def init() -> None:
    with db.tx() as c:
        c.executescript(SCHEMA)
    r = db.q1("SELECT COUNT(*) AS n, MAX(updated) AS u FROM stock_sector")
    STATE.update(map_count=r["n"], map_updated=r["u"])


# ----------------------------------------------------------------------------- 新浪板块
def _parse_sina_var(text: str) -> list[dict]:
    m = re.search(r"=\s*(\{.*\})", text, re.S)
    if not m:
        return []
    d = json.loads(m.group(1))
    out = []
    for key, v in d.items():
        p = v.split(",")
        if len(p) < 13:
            continue
        out.append({"code": p[0] or key, "name": p[1], "count": int(market._f(p[2], 0) or 0),
                    "avg_price": market._f(p[3]), "pct": round(market._f(p[5], 0) or 0, 2),
                    "amount": market._f(p[7], 0) or 0, "leader_code": p[8], "leader_pct": market._f(p[9]),
                    "leader_price": market._f(p[10]), "leader_name": p[12]})
    return out


def sina_boards(kind: str = "industry") -> list[dict]:
    """kind: industry 行业 / concept 概念。缓存 60 秒（盘中）。"""
    if market.MOCK:
        return []
    url = (SINA + "/q/view/newSinaHy.php") if kind == "industry" else \
        "https://money.finance.sina.com.cn/q/view/newFLJK.php?param=class"

    def load():
        raw = market.http_get(url, "https://finance.sina.com.cn/", retries=1).decode("gbk", "ignore")
        return _parse_sina_var(raw)
    ttl = 60 if market.market_phase() in ("trading", "auction", "close_auction") else 600
    return market._cached("sina_" + kind, ttl, load)


def members(node: str, max_pages: int = 6) -> list[str]:
    """板块成分股代码（新浪 node，如 new_blhy / gn_hwqc）。"""
    if market.MOCK:
        return [r["code"] for r in db.q("SELECT code FROM stock_sector WHERE ind_code=?", (node,))]

    def load():
        out = []
        for page in range(1, max_pages + 1):
            url = (SINA + "/quotes_service/api/json_v2.php/Market_Center.getHQNodeData?page=%d&num=100&sort=symbol"
                   "&asc=1&node=%s&symbol=&_s_r_a=init" % (page, node))
            rows = json.loads(market.http_get(url, "https://vip.stock.finance.sina.com.cn/", retries=1) or b"[]")
            out += [r["symbol"] for r in rows if r.get("symbol")]
            if len(rows) < 100:
                break
        return out
    return market._cached("members_" + node, 1800, load)


# ----------------------------------------------------------------------------- 个股 → 行业
def refresh_map(force: bool = False) -> dict:
    """每周刷新一次个股所属行业（约 60 次请求，后台慢速执行）。"""
    if not force and STATE["map_updated"] and STATE["map_count"] > 1000:
        last = STATE["map_updated"]
        if (now().date() - __import__("datetime").date.fromisoformat(last)).days < 7:
            return {"skipped": True}
    with _lock:
        if STATE["map_running"]:
            return {"skipped": "running"}
        STATE["map_running"] = True
    try:
        rows = []
        if market.MOCK:
            for u in db.q("SELECT code FROM universe"):
                i = sum(map(ord, u["code"])) % len(MOCK_INDUSTRIES)
                rows.append((u["code"], MOCK_INDUSTRIES[i], f"mock_{i}", today()))
        else:
            for b in sina_boards("industry"):
                try:
                    for code in members(b["code"]):
                        if market.is_main_board(code):
                            rows.append((code, b["name"], b["code"], today()))
                except (RuntimeError, ValueError) as e:
                    STATE["error"] = f"{b['name']}: {e}"[:200]
                time.sleep(0.3)
        if len(rows) > 200:
            with db.tx() as c:
                c.executemany("INSERT INTO stock_sector VALUES(?,?,?,?) ON CONFLICT(code) DO UPDATE SET "
                              "industry=excluded.industry, ind_code=excluded.ind_code, updated=excluded.updated", rows)
            STATE.update(map_updated=today(), map_count=len(rows), error=None)
            db.event("system", "行业分类已更新", f"{len(rows)} 只主板股票归入 {len({r[2] for r in rows})} 个行业")
        _mem["t"] = 0
        return {"count": len(rows)}
    finally:
        STATE["map_running"] = False


def refresh_map_async(force: bool = False):
    threading.Thread(target=lambda: _safe(refresh_map, force), daemon=True, name="aq5-sectormap").start()


def _safe(fn, *a):
    try:
        fn(*a)
    except Exception as e:  # noqa: BLE001
        STATE["error"] = str(e)[:200]


def _map() -> dict:
    return {r["code"]: (r["industry"], r["ind_code"]) for r in db.q("SELECT code, industry, ind_code FROM stock_sector")}


# ----------------------------------------------------------------------------- 行业强度
def _pctile(vals: list[float]) -> list[float]:
    from .learn import _rank
    if len(vals) < 2:
        return [50.0] * len(vals)
    r = _rank(vals)
    return [100.0 * x / (len(vals) - 1) for x in r]


def compute(force: bool = False) -> dict:
    """返回 {"industries": [...按强度排序], "by_code": {ind_code: 行业}, "stock": {code: ind_code}}，缓存 60 秒。"""
    if not force and _mem["data"] and time.time() - _mem["t"] < 60:
        return _mem["data"]
    mp = _map()
    snap = market.snapshot()
    groups: dict[str, dict] = {}
    for r in snap:
        ind = mp.get(r["code"])
        if not ind or r.get("pct") is None:
            continue
        g = groups.setdefault(ind[1], {"code": ind[1], "name": ind[0], "rows": []})
        g["rows"].append(r)
    sina = {b["code"]: b for b in (sina_boards("industry") if groups else [])}
    hist = {}
    for h in db.q("SELECT ind_code, date, pct FROM sector_daily WHERE date<? ORDER BY date DESC", (today(),)):
        hist.setdefault(h["ind_code"], []).append(h["pct"])
    inds = []
    for g in groups.values():
        rows = g.pop("rows")
        if len(rows) < 3:
            continue
        pcts = sorted(x["pct"] for x in rows)
        k = max(1, len(pcts) // 10)
        core = pcts[k:-k] if len(pcts) > 2 * k + 2 else pcts  # 去掉两头 10%，避免个别股票扭曲
        lead = max(rows, key=lambda x: x["pct"])
        lu = sum(1 for x in rows if x.get("prev") and x.get("price") and
                 x["price"] >= round(x["prev"] * (1 + market.limit_pct(x["code"], x.get("name"))) - 0.005, 2))
        h = hist.get(g["code"], [])[:4]
        sb = sina.get(g["code"], {})
        inds.append({**g, "n": len(rows), "pct": round(sum(core) / len(core), 2),
                     "up_ratio": round(sum(1 for x in rows if x["pct"] > 0) / len(rows), 3), "limit_up": lu,
                     "amount_yi": round(sum(x.get("amount") or 0 for x in rows) / 1e8, 1),
                     "main_net_yi": round(sum(x.get("main_net") or 0 for x in rows) / 1e8, 2)
                     if any(x.get("main_net") for x in rows) else None,
                     "mom5": round(sum(core) / len(core) + sum(h), 2) if len(h) >= 2 else None, "hist_days": len(h),
                     "leader": {"code": lead["code"], "name": lead.get("name"), "pct": lead["pct"]},
                     "sina_pct": sb.get("pct")})
    if inds:
        p_pct = _pctile([x["pct"] for x in inds])
        p_up = _pctile([x["up_ratio"] for x in inds])
        p_lu = _pctile([x["limit_up"] for x in inds])
        has_mom = sum(1 for x in inds if x["mom5"] is not None) > len(inds) * 0.6
        p_mom = _pctile([x["mom5"] if x["mom5"] is not None else x["pct"] for x in inds]) if has_mom else None
        for i, x in enumerate(inds):
            if has_mom:
                s = 0.40 * p_pct[i] + 0.20 * p_up[i] + 0.15 * p_lu[i] + 0.25 * p_mom[i]
            else:
                s = 0.55 * p_pct[i] + 0.30 * p_up[i] + 0.15 * p_lu[i]
            x["score"] = round(s, 1)
            x["tag"] = "领涨" if s >= 85 else "强势" if s >= 65 else "中性" if s >= 35 else "弱势" if s >= 15 else "领跌"
        inds.sort(key=lambda x: -x["score"])
        for i, x in enumerate(inds):
            x["rank"] = i + 1
    data = {"industries": inds, "by_code": {x["code"]: x for x in inds},
            "stock": {c: v[1] for c, v in mp.items()}, "time": now().strftime("%H:%M:%S"),
            "map": {"count": STATE["map_count"], "updated": STATE["map_updated"], "running": STATE["map_running"]}}
    _mem.update(t=time.time(), data=data)
    return data


def record_daily() -> int:
    """收盘后（15:05）记下各行业当日表现，供“近5日”动量使用。"""
    d = compute(force=True)
    with db.tx() as c:
        c.executemany("INSERT OR REPLACE INTO sector_daily VALUES(?,?,?,?,?,?,?,?)",
                      [(today(), x["code"], x["name"], x["pct"], x["up_ratio"], x["limit_up"], x["amount_yi"],
                        x["score"]) for x in d["industries"]])
    return len(d["industries"])


def of_stock(code: str) -> dict | None:
    """个股所属行业及强度；不知道行业返回 None。"""
    try:
        d = compute()
    except Exception:  # noqa: BLE001
        return None
    ind = d["by_code"].get(d["stock"].get(market.norm_code(code), ""))
    if not ind:
        return None
    return {"code": ind["code"], "name": ind["name"], "score": ind["score"], "rank": ind["rank"],
            "total": len(d["industries"]), "pct": ind["pct"], "tag": ind["tag"], "leader": ind["leader"],
            "up_ratio": ind["up_ratio"], "limit_up": ind["limit_up"], "mom5": ind["mom5"]}


def strength(code: str) -> float | None:
    s = of_stock(code)
    return s["score"] if s else None


# ----------------------------------------------------------------------------- 对外视图
def view() -> dict:
    d = compute()
    try:
        con = sorted(sina_boards("concept"), key=lambda b: -(b["pct"] or 0))
    except (RuntimeError, ValueError):
        con = []
    strip = lambda x: {k: v for k, v in x.items() if k != "rows"}  # noqa: E731
    return {"time": d["time"], "map": d["map"], "industries": [strip(x) for x in d["industries"]],
            "concepts_top": con[:20], "concepts_bottom": con[-8:][::-1] if len(con) > 28 else [],
            "concept_total": len(con)}


def detail(node: str) -> dict:
    """板块成分股（按今日涨幅排序），行业用本地对应关系，概念现场向新浪取。"""
    snap = {r["code"]: r for r in market.snapshot()}
    if node.startswith(("new_", "mock_")):
        codes = [r["code"] for r in db.q("SELECT code FROM stock_sector WHERE ind_code=?", (node,))]
        info = compute()["by_code"].get(node, {})
        name = info.get("name") or node
    else:
        codes = [c for c in members(node) if market.is_main_board(c)]
        b = next((x for x in sina_boards("concept") if x["code"] == node), {})
        info, name = b, b.get("name", node)
    rows = [snap[c] for c in codes if c in snap]
    rows.sort(key=lambda r: -(r.get("pct") or 0))
    keep = ("code", "name", "price", "pct", "amount", "turnover", "vr", "main_net", "mcap")
    return {"code": node, "name": name, "info": {k: v for k, v in info.items() if k != "rows"},
            "stocks": [{k: r.get(k) for k in keep} for r in rows[:80]], "total": len(codes)}


def brief(n: int = 6) -> dict:
    """给 AI 用的板块摘要。"""
    try:
        v = view()
    except Exception:  # noqa: BLE001
        return {}
    f = lambda x: f"{x['name']}{x['pct']:+.2f}%(强度{x['score']:.0f},涨停{x['limit_up']},领涨{x['leader']['name']})"  # noqa: E731
    return {"最强行业": [f(x) for x in v["industries"][:n]], "最弱行业": [f(x) for x in v["industries"][-3:]],
            "热门概念": [f"{c['name']}{c['pct']:+.2f}%(领涨{c['leader_name']})" for c in v["concepts_top"][:n]]}
