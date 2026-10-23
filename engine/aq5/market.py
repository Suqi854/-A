"""行情数据层：腾讯（报价/日K/分时）+ 东方财富（全市场快照/主力资金）。

所有接口均为公开网页接口；失败时自动重试/切换，并把日K缓存进 SQLite。
AQ5_MOCK=1 时使用内置模拟行情（离线测试、演示用）。
"""
from __future__ import annotations

import gzip
import json
import random
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from . import db
from .config import MOCK, load_settings, now, today

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"
_pool = ThreadPoolExecutor(max_workers=8)
_cache: dict[str, tuple[float, object]] = {}
_cache_lock = threading.Lock()


def _cached(key: str, ttl: float, fn):
    with _cache_lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
    val = fn()
    with _cache_lock:
        _cache[key] = (time.time(), val)
    return val


class Blocked(RuntimeError):
    """数据源的防火墙拦截了本机（请求太频繁），一段时间内暂停访问该站点。"""


_host_lock = threading.Lock()
_host_state: dict[str, dict] = {}   # host -> {"next": 时间戳, "blocked_until": 时间戳}
_host_sem: dict[str, threading.Semaphore] = {}
MIN_INTERVAL = {"web.ifzq.gtimg.cn": 0.35, "push2his.eastmoney.com": 0.4, "push2.eastmoney.com": 0.3,
                "qt.gtimg.cn": 0.05, "quotes.sina.cn": 0.4}


def host_status() -> dict:
    t = time.time()
    return {h: round(v["blocked_until"] - t) for h, v in _host_state.items() if v.get("blocked_until", 0) > t}


def _throttle(host: str):
    with _host_lock:
        st = _host_state.setdefault(host, {"next": 0.0, "blocked_until": 0.0})
        if st["blocked_until"] > time.time():
            raise Blocked(f"{host} 暂时被限流，{int(st['blocked_until'] - time.time())} 秒后再试")
        wait = max(0.0, st["next"] - time.time())
        st["next"] = max(st["next"], time.time()) + MIN_INTERVAL.get(host, 0.1)
        sem = _host_sem.setdefault(host, threading.Semaphore(2 if host in MIN_INTERVAL else 4))
    if wait:
        time.sleep(wait)
    return sem


def _block(host: str, secs: int):
    with _host_lock:
        _host_state.setdefault(host, {"next": 0.0, "blocked_until": 0.0})["blocked_until"] = time.time() + secs
    try:
        db.event("system", f"数据源 {host} 拦截了请求，暂停 {secs // 60} 分钟", "请求太频繁触发了对方的防火墙，已自动降速并切换备用数据源", "warn")
    except Exception:  # noqa: BLE001
        pass


# 国内行情站点默认直连：系统代理（如 Clash 走境外节点）会被东财等站点直接断开连接
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_PROXY = urllib.request.build_opener()
CN_HOSTS = ("eastmoney.com", "gtimg.cn", "sina.com.cn", "sina.cn", "sinajs.cn", "qq.com", "10jqka.com.cn")


_net_cfg = {"t": 0.0, "direct": True}


def _opener_for(url: str):
    host = urllib.parse.urlsplit(url).hostname or ""
    if time.time() - _net_cfg["t"] > 15:
        try:
            from .config import load_settings
            _net_cfg.update(t=time.time(), direct=load_settings().get("net", {}).get("direct_cn", True))
        except Exception:  # noqa: BLE001
            pass
    direct = _net_cfg["direct"]
    return _DIRECT if direct and host.endswith(CN_HOSTS) else _PROXY


def http_get(url: str, referer: str = "https://finance.qq.com", timeout: float = 8, retries: int = 2) -> bytes:
    host = urllib.parse.urlsplit(url).hostname or ""
    last = None
    for i in range(retries + 1):
        sem = _throttle(host)
        try:
            with sem:
                req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": referer,
                                                           "Accept-Encoding": "gzip"})
                with _opener_for(url).open(req, timeout=timeout) as r:
                    final = r.geturl()
                    raw = r.read()
                    if r.headers.get("Content-Encoding") == "gzip":
                        raw = gzip.decompress(raw)
            if "waf.tencent.com" in final or raw[:200].lstrip().startswith(b"<") and b"501" in raw[:3000]:
                _block(host, 900)
                raise Blocked(f"{host} 防火墙拦截")
            return raw
        except Blocked:
            raise
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (403, 429, 501):
                _block(host, 900)
                raise Blocked(f"{host} 返回 {e.code}")
            time.sleep(0.6 * (i + 1))
        except Exception as e:  # noqa: BLE001 - 网络错误种类很多，统一重试
            last = e
            time.sleep(0.6 * (i + 1))
    raise RuntimeError(f"请求失败: {url[:80]} ({last})")


def _f(x, default=None):
    try:
        if x in ("", "-", None):
            return default
        return float(x)
    except (TypeError, ValueError):
        return default


def norm_code(code: str) -> str:
    """'600519' / 'SH600519' / 'sh600519' -> 'sh600519'。"""
    c = code.strip().lower()
    if re.fullmatch(r"(sh|sz|bj)\d{6}", c):
        return c
    if re.fullmatch(r"\d{6}", c):
        return ("sh" if c[0] in "65" else "bj" if c[0] in "48" else "sz") + c
    if c.startswith(("hk", "us")):
        return c[:2] + c[2:].upper()
    return c


def is_main_board(code: str) -> bool:
    d = code[2:]
    return (code.startswith("sh") and d[:3] in ("600", "601", "603", "605")) or \
           (code.startswith("sz") and d[:3] in ("000", "001", "002", "003"))


def limit_pct(code: str, name: str = "") -> float:
    if "ST" in (name or "").upper():
        return 0.05
    d = code[2:]
    if d.startswith(("300", "301", "688", "689")):
        return 0.20
    return 0.10


# ----------------------------------------------------------------------------- 腾讯实时报价
def _parse_tencent(text: str) -> dict:
    out = {}
    for m in re.finditer(r'v_(\w+)="([^"]*)"', text):
        key, body = m.group(1), m.group(2)
        f = body.split("~")
        if len(f) < 35:
            continue
        code = key.replace("s_", "")
        if key.startswith(("hk", "us")):
            q = {"code": key, "name": f[1], "price": _f(f[3]), "prev": _f(f[4]), "open": _f(f[5]),
                 "chg": _f(f[31]), "pct": _f(f[32]), "high": _f(f[33]), "low": _f(f[34]), "time": f[30]}
        else:
            q = {"code": code, "name": f[1], "price": _f(f[3]), "prev": _f(f[4]), "open": _f(f[5]),
                 "vol": _f(f[6], 0) * 100, "outer": _f(f[7], 0), "inner": _f(f[8], 0),
                 "bids": [[_f(f[9 + 2 * i]), _f(f[10 + 2 * i], 0)] for i in range(5)],
                 "asks": [[_f(f[19 + 2 * i]), _f(f[20 + 2 * i], 0)] for i in range(5)],
                 "time": f[30], "chg": _f(f[31]), "pct": _f(f[32]), "high": _f(f[33]), "low": _f(f[34]),
                 "amount": _f(f[37], 0) * 1e4, "turnover": _f(f[38]), "pe": _f(f[39]),
                 "amp": _f(f[43]), "fmcap": _f(f[44]), "mcap": _f(f[45]), "pb": _f(f[46]),
                 "up_limit": _f(f[47]), "dn_limit": _f(f[48]), "vr": _f(f[49]),
                 "avg": _f(f[51]) if len(f) > 51 else None}
        if q["price"] in (None, 0) and q.get("prev"):
            q["price"] = q["prev"]  # 停牌或竞价前
            q["suspended"] = True
        out[code] = q
    return out


def quotes(codes: list[str]) -> dict:
    codes = [norm_code(c) for c in codes if c]
    if not codes:
        return {}
    if MOCK:
        return mock.quotes(codes)
    key = "q:" + ",".join(sorted(codes))

    def load():
        res = {}
        batches = [codes[i:i + 60] for i in range(0, len(codes), 60)]

        def one(b):
            try:
                return _parse_tencent(http_get("https://qt.gtimg.cn/q=" + ",".join(b)).decode("gbk", "ignore"))
            except RuntimeError:
                return {}
        for part in _pool.map(one, batches):
            res.update(part)
        return res
    return _cached(key, 2.0, load)


# ----------------------------------------------------------------------------- 东财全市场快照
EM_FIELDS = "f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13,f14,f15,f16,f17,f18,f20,f21,f22,f23,f26,f62,f184"


def _em_page(pn: int) -> tuple[int, list]:
    url = ("https://push2.eastmoney.com/api/qt/clist/get?pn=%d&pz=100&po=1&np=1&fltt=2&invt=2&fid=f12"
           "&fs=m:0+t:6,m:1+t:2&fields=%s" % (pn, EM_FIELDS))
    d = json.loads(http_get(url, "https://quote.eastmoney.com/", timeout=6, retries=1))
    data = d.get("data") or {}
    return int(data.get("total") or 0), data.get("diff") or []


def _em_row(r: dict) -> dict | None:
    code = ("sh" if r.get("f13") == 1 else "sz") + str(r.get("f12"))
    if not is_main_board(code):
        return None
    ld = str(r.get("f26") or "")
    return {"code": code, "name": r.get("f14"), "price": _f(r.get("f2")), "pct": _f(r.get("f3")),
            "chg": _f(r.get("f4")), "vol": (_f(r.get("f5"), 0) or 0) * 100, "amount": _f(r.get("f6"), 0),
            "amp": _f(r.get("f7")), "turnover": _f(r.get("f8")), "pe": _f(r.get("f9")), "vr": _f(r.get("f10")),
            "speed5": _f(r.get("f11")), "high": _f(r.get("f15")), "low": _f(r.get("f16")),
            "open": _f(r.get("f17")), "prev": _f(r.get("f18")), "mcap": (_f(r.get("f20"), 0) or 0) / 1e8,
            "fmcap": (_f(r.get("f21"), 0) or 0) / 1e8, "speed": _f(r.get("f22")), "pb": _f(r.get("f23")),
            "listed": f"{ld[:4]}-{ld[4:6]}-{ld[6:8]}" if len(ld) == 8 else None,
            "main_net": _f(r.get("f62")), "main_pct": _f(r.get("f184"))}


_em_state = {"bad_until": 0.0}


def _main_board_codes() -> list[str]:
    """沪深主板全部可能代码（腾讯不存在的代码会被自动忽略）。"""
    sh = [f"sh{p}{i:03d}" for p in ("600", "601", "603", "605") for i in range(1000)]
    sz = [f"sz{p}{i:03d}" for p in ("000", "001", "002", "003") for i in range(1000)]
    return sh + sz


def _tencent_snapshot() -> dict:
    """东财不可用时的备用：腾讯批量报价扫全主板（无主力资金字段）。"""
    uni = [u["code"] for u in db.q("SELECT code FROM universe")]
    codes = uni if len(uni) > 2500 else _main_board_codes()
    out = {}
    batches = [codes[i:i + 80] for i in range(0, len(codes), 80)]

    def one(b):
        try:
            return _parse_tencent(http_get("https://qt.gtimg.cn/q=" + ",".join(b), retries=1).decode("gbk", "ignore"))
        except RuntimeError:
            return {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        for part in ex.map(one, batches):
            for c, qd in part.items():
                if is_main_board(c) and qd.get("name"):
                    out[c] = {**qd, "main_net": None, "main_pct": None, "listed": None}
    return out


def _em_snapshot() -> dict:
    total, first = _em_page(1)
    rows = list(first)
    pages = max(1, (total + 99) // 100)
    failed = []

    def one(pn):
        time.sleep(0.15 * (pn % 3))
        try:
            return pn, _em_page(pn)[1]
        except RuntimeError:
            return pn, None
    with ThreadPoolExecutor(max_workers=3) as ex:
        for pn, part in ex.map(one, range(2, pages + 1)):
            if part is None:
                failed.append(pn)
            else:
                rows.extend(part)
    for pn in failed:  # 慢速重试一次
        time.sleep(1.0)
        try:
            rows.extend(_em_page(pn)[1])
        except RuntimeError:
            pass
    out = {r["code"]: r for r in (_em_row(x) for x in rows) if r}
    uni = [u["code"] for u in db.q("SELECT code FROM universe")]
    missing = [c for c in uni if c not in out]
    if missing:  # 东财缺页 -> 腾讯补
        for c, qd in quotes(missing).items():
            if is_main_board(c):
                out[c] = {**qd, "main_net": None, "main_pct": None, "listed": None}
    return out


SNAP_INFO = {"source": None, "time": None}


def snapshot(force: bool = False) -> list[dict]:
    """沪深主板全市场实时快照（约3000+只）。东财为主（含主力资金）；东财连不上时自动改用腾讯。"""
    if MOCK:
        return mock.snapshot()

    def load():
        out, src = {}, "eastmoney"
        if time.time() > _em_state["bad_until"]:
            try:
                out = _em_snapshot()
            except RuntimeError:
                _em_state["bad_until"] = time.time() + 600  # 10 分钟内不再尝试东财
        if len(out) < 1000:
            src = "tencent"
            out = {**_tencent_snapshot(), **out}
        if not out:
            raise RuntimeError("东财和腾讯的全市场行情都取不到，请检查网络")
        SNAP_INFO.update(source=src, time=now().strftime("%H:%M:%S"))
        with db.tx() as c:
            c.executemany("INSERT INTO universe(code,name,mcap,fmcap,updated) VALUES(?,?,?,?,?) "
                          "ON CONFLICT(code) DO UPDATE SET name=excluded.name, mcap=excluded.mcap, "
                          "fmcap=excluded.fmcap, updated=excluded.updated",
                          [(r["code"], r["name"], r.get("mcap"), r.get("fmcap"), today()) for r in out.values()])
        return list(out.values())
    ttl = 30.0 if market_phase() in ("trading", "auction", "pre", "close_auction") else 300.0
    return _cached("snapshot", 0 if force else ttl, load)


def breadth(rows: list[dict]) -> dict:
    up = sum(1 for r in rows if (r.get("pct") or 0) > 0)
    dn = sum(1 for r in rows if (r.get("pct") or 0) < 0)
    lu = sum(1 for r in rows if r.get("prev") and r.get("price") and
             r["price"] >= round(r["prev"] * (1 + limit_pct(r["code"], r.get("name"))) - 0.005, 2))
    ld = sum(1 for r in rows if r.get("prev") and r.get("price") and
             r["price"] <= round(r["prev"] * (1 - limit_pct(r["code"], r.get("name"))) + 0.005, 2))
    amt = sum(r.get("amount") or 0 for r in rows)
    main = sum(r.get("main_net") or 0 for r in rows)
    pcts = sorted(r["pct"] for r in rows if r.get("pct") is not None)
    med = pcts[len(pcts) // 2] if pcts else 0
    buckets = [0] * 11  # <-7,-7..-5,-5..-3,-3..-1,-1..0,0,0..1,1..3,3..5,5..7,>7
    edges = [-7, -5, -3, -1, -0.0001, 0.0001, 1, 3, 5, 7]
    for p in pcts:
        i = 0
        while i < len(edges) and p > edges[i]:
            i += 1
        buckets[i] += 1
    return {"up": up, "down": dn, "flat": len(rows) - up - dn, "limit_up": lu, "limit_down": ld,
            "amount_yi": round(amt / 1e8, 1), "main_net_yi": round(main / 1e8, 2), "median_pct": med,
            "total": len(rows), "dist": buckets}


# ----------------------------------------------------------------------------- 日K（前复权）
def _kline_tencent(code: str, n: int) -> list[dict]:
    d = json.loads(http_get(f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,{n},qfq"))
    node = (d.get("data") or {}).get(code) or {}
    arr = node.get("qfqday") or node.get("day") or []
    return [{"date": a[0], "open": float(a[1]), "close": float(a[2]), "high": float(a[3]), "low": float(a[4]),
             "vol": float(a[5]) * 100} for a in arr if len(a) >= 6]


def _kline_eastmoney(code: str, n: int) -> list[dict]:
    secid = ("1." if code.startswith("sh") else "0.") + code[2:]
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=%s&fields1=f1,f2,f3"
           "&fields2=f51,f52,f53,f54,f55,f56&klt=101&fqt=1&lmt=%d&end=20500101" % (secid, n))
    d = json.loads(http_get(url, "https://quote.eastmoney.com/", timeout=8, retries=1))
    out = []
    for s_ in ((d.get("data") or {}).get("klines") or []):
        p = s_.split(",")
        out.append({"date": p[0], "open": float(p[1]), "close": float(p[2]), "high": float(p[3]), "low": float(p[4]),
                    "vol": float(p[5]) * 100})
    return out


# ----------------------------------------------------------------------------- 多数据源：同花顺 / 新浪 / 腾讯 / 东财
SRC_STATS: dict[str, dict] = {}


def _stat(src: str, ok: bool, err: str = "") -> None:
    st = SRC_STATS.setdefault(src, {"ok": 0, "fail": 0, "last_ok": None, "last_err": None})
    if ok:
        st["ok"] += 1
        st["last_ok"] = now().strftime("%H:%M:%S")
    else:
        st["fail"] += 1
        st["last_err"] = (now().strftime("%H:%M:%S") + " " + err)[:160]


def _jsonp(raw: bytes) -> dict:
    t = raw.decode("utf-8", "ignore")
    return json.loads(t[t.index("(") + 1: t.rindex(")")])


THS_TYPE = {"day": "01", "week": "11", "month": "21"}


def ths_code(code: str) -> str:
    """同花顺代码：个股 600519；上证指数 1A0001；其他上证指数 1B0300；深证指数 399001。"""
    if code == "sh000001":
        return "1A0001"
    if code.startswith("sh000"):
        return "1B" + code[4:]
    return code[2:]


def _kline_ths(code: str, n: int, period: str = "day") -> list[dict]:
    """同花顺 d.10jqka.com.cn：前复权日/周/月K（last.js 约最近 140 根，不够再取往年文件）。"""
    t = THS_TYPE[period]
    base = f"https://d.10jqka.com.cn/v6/line/hs_{ths_code(code)}/{t}/"
    ref = "https://stockpage.10jqka.com.cn/"
    d = _jsonp(http_get(base + "last.js", ref, retries=1))
    chunks = [d.get("data") or ""]
    have = chunks[0].count(";") + 1 if chunks[0] else 0
    if period == "day" and have < n:
        y = now().year
        first_year = min((int(k) for k in (d.get("year") or {}) if str(k).isdigit()), default=y)
        while have < n and y >= first_year and y > now().year - 3:
            try:
                yd = _jsonp(http_get(base + f"{y}.js", ref, retries=1))
                chunks.insert(0, yd.get("data") or "")
                have += (yd.get("data") or "").count(";") + 1
            except (RuntimeError, ValueError):
                break
            y -= 1
    rows = {}
    for chunk in chunks:
        for item in chunk.split(";"):
            p = item.split(",")
            if len(p) < 6 or not p[1]:
                continue
            dt = f"{p[0][:4]}-{p[0][4:6]}-{p[0][6:8]}"
            rows[dt] = {"date": dt, "open": float(p[1]), "high": float(p[2]), "low": float(p[3]),
                        "close": float(p[4]), "vol": float(p[5])}
    return [rows[k] for k in sorted(rows)][-n:]


def _kline_tencent_p(code: str, n: int, period: str) -> list[dict]:
    d = json.loads(http_get(f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},{period},,,{n},qfq"))
    node = (d.get("data") or {}).get(code) or {}
    arr = node.get("qfq" + period) or node.get(period) or []
    return [{"date": a[0], "open": float(a[1]), "close": float(a[2]), "high": float(a[3]), "low": float(a[4]),
             "vol": float(a[5]) * 100} for a in arr if len(a) >= 6]


def _kline_sina(code: str, n: int, scale: int = 240) -> list[dict]:
    """新浪：分钟K（5/15/30/60）与日K（240，不复权，仅作最后备用）。"""
    url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
           f"?symbol={code}&scale={scale}&ma=no&datalen={n}")
    arr = json.loads(http_get(url, "https://finance.sina.com.cn/", retries=1) or b"[]") or []
    return [{"date": a["day"], "open": float(a["open"]), "close": float(a["close"]), "high": float(a["high"]),
             "low": float(a["low"]), "vol": float(a["volume"])} for a in arr]


def _kline_tencent_m(code: str, n: int, m: int) -> list[dict]:
    d = json.loads(http_get(f"https://web.ifzq.gtimg.cn/appstock/app/kline/mkline?param={code},m{m},,{n}"))
    arr = ((d.get("data") or {}).get(code) or {}).get(f"m{m}") or []
    out = []
    for a in arr:
        t = str(a[0])
        out.append({"date": f"{t[:4]}-{t[4:6]}-{t[6:8]} {t[8:10]}:{t[10:12]}", "open": float(a[1]),
                    "close": float(a[2]), "high": float(a[3]), "low": float(a[4]), "vol": float(a[5]) * 100})
    return out


def _try_sources(label: str, fns: list) -> tuple[list[dict], str]:
    last = None
    for name, fn in fns:
        try:
            rows = fn()
            if rows:
                _stat(name, True)
                return rows, name
            _stat(name, False, "空数据")
        except (RuntimeError, ValueError, KeyError, IndexError, TypeError) as e:
            _stat(name, False, str(e))
            last = e
    raise RuntimeError(f"{label} 所有数据源都失败：{last}")


def _fetch_kline(code: str, n: int) -> list[dict]:
    """前复权日K：腾讯 → 同花顺 → 东财 → 新浪（不复权）依次尝试。"""
    rows, _ = _try_sources("日K " + code, [("腾讯", lambda: _kline_tencent(code, n)),
                                          ("同花顺", lambda: _kline_ths(code, n, "day")),
                                          ("东财", lambda: _kline_eastmoney(code, n)),
                                          ("新浪", lambda: _kline_sina(code, n, 240))])
    return rows


PERIODS = {"day": "日K", "week": "周K", "month": "月K", "m60": "60分", "m30": "30分", "m15": "15分", "m5": "5分"}


def kline_period(code: str, period: str = "day", n: int = 320) -> dict:
    """任意周期 K 线。日K走本地缓存；周/月K 腾讯→同花顺；分钟K 腾讯→新浪。"""
    code = norm_code(code)
    if period == "day":
        return {"code": code, "period": period, "rows": kline_with_live(code, n), "source": "本地缓存"}
    if MOCK:
        rows = mock.kline(code, n * (5 if period == "week" else 21 if period == "month" else 1))
        if period in ("week", "month"):
            agg, key = {}, (lambda d: d[:7]) if period == "month" else (lambda d: datetime.strptime(d, "%Y-%m-%d").strftime("%G-%V"))
            for r in rows:
                k = key(r["date"])
                a = agg.get(k)
                if not a:
                    agg[k] = dict(r)
                else:
                    a.update(date=r["date"], close=r["close"], high=max(a["high"], r["high"]),
                             low=min(a["low"], r["low"]), vol=a["vol"] + r["vol"])
            rows = list(agg.values())[-n:]
        return {"code": code, "period": period, "rows": rows, "source": "模拟"}

    def load():
        if period in ("week", "month"):
            rows, src = _try_sources(PERIODS[period], [("腾讯", lambda: _kline_tencent_p(code, n, period)),
                                                         ("同花顺", lambda: _kline_ths(code, n, period))])
        else:
            m = int(period[1:])
            rows, src = _try_sources(PERIODS[period], [("腾讯", lambda: _kline_tencent_m(code, n, m)),
                                                         ("新浪", lambda: _kline_sina(code, n, m))])
        return {"code": code, "period": period, "rows": rows, "source": src}
    ttl = 60 if period.startswith("m") else 600
    return _cached(f"kp:{code}:{period}:{n}", ttl, load)


def cross_check(code: str) -> dict:
    """多源校验：腾讯 vs 同花顺 最近 5 根日K收盘价是否一致（缓存 1 小时）。"""
    code = norm_code(code)
    if MOCK:
        return {"ok": True, "sources": ["模拟"], "max_diff_pct": 0.0, "note": "模拟数据"}

    def load():
        got = {}
        for name, fn in (("腾讯", lambda: _kline_tencent(code, 8)), ("同花顺", lambda: _kline_ths(code, 8, "day"))):
            try:
                got[name] = {r["date"]: r["close"] for r in fn()[-6:]}
                _stat(name, True)
            except (RuntimeError, ValueError, KeyError, IndexError) as e:
                _stat(name, False, str(e))
        if len(got) < 2:
            return {"ok": None, "sources": list(got), "max_diff_pct": None, "note": "只有一个数据源可用，无法交叉校验"}
        a, b = got["腾讯"], got["同花顺"]
        common = sorted(set(a) & set(b))[-5:]
        if not common:
            return {"ok": None, "sources": list(got), "max_diff_pct": None, "note": "两个数据源日期对不上"}
        diff = max(abs(a[d] / b[d] - 1) * 100 for d in common)
        return {"ok": diff < 0.3, "sources": list(got), "max_diff_pct": round(diff, 3), "days": len(common),
                "note": f"腾讯与同花顺最近 {len(common)} 日收盘价最大偏差 {diff:.3f}%" + ("，一致" if diff < 0.3 else "，存在差异（可能是复权口径不同）")}
    return _cached("xc:" + code, 3600, load)


def _last_close_stamp() -> str:
    """最近一个已收盘交易日的 'YYYY-MM-DD 15:05'（按工作日估算）。"""
    t = now()
    d = t.date()
    if t.hour * 100 + t.minute < 1505 or t.weekday() >= 5:
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.isoformat() + " 15:05"


def kline(code: str, n: int = 250, refresh: bool = False, history: bool = False) -> list[dict]:
    code = norm_code(code)
    if MOCK:
        return mock.kline(code, n)
    meta = db.q1("SELECT updated, n FROM kline_meta WHERE code=?", (code,))
    t = now()
    fresh = False
    if meta and meta["n"] >= min(n, 120):
        if history:  # 只需要截至上一个收盘日的历史（选股、学习用），一天只拉一次
            fresh = meta["updated"] >= _last_close_stamp()
        elif meta["updated"][:10] == today():
            if t.hour * 100 + t.minute >= 1505:
                fresh = meta["updated"][11:] >= "15:05"          # 收盘后需要拿到终版日线
            else:
                fresh = meta["updated"][11:13] == t.strftime("%H")  # 盘中每小时更新一次
    if refresh or not fresh:
        try:
            rows = _fetch_kline(code, max(n, 250))
            if rows:
                with db.tx() as c:
                    c.executemany("INSERT OR REPLACE INTO klines VALUES(?,?,?,?,?,?,?)",
                                  [(code, r["date"], r["open"], r["close"], r["high"], r["low"], r["vol"])
                                   for r in rows])
                    c.execute("INSERT OR REPLACE INTO kline_meta VALUES(?,?,?)",
                              (code, now().strftime("%Y-%m-%d %H:%M"), len(rows)))
        except RuntimeError:
            pass
    rows = db.q("SELECT date, open, close, high, low, vol FROM klines WHERE code=? ORDER BY date DESC LIMIT ?",
                (code, n))
    return rows[::-1]


def kline_with_live(code: str, n: int = 250) -> list[dict]:
    """历史日K + 盘中实时一根（若今天还没入库）。"""
    rows = kline(code, n)
    qd = quotes([code]).get(norm_code(code))
    if qd and qd.get("price") and not qd.get("suspended"):
        d = today()
        bar = {"date": d, "open": qd.get("open") or qd["price"], "close": qd["price"],
               "high": qd.get("high") or qd["price"], "low": qd.get("low") or qd["price"], "vol": qd.get("vol") or 0}
        if rows and rows[-1]["date"] == d:
            rows[-1] = bar
        elif not rows or rows[-1]["date"] < d:
            if is_trading_day_live(qd):
                rows.append(bar)
    return rows


def is_trading_day_live(qd: dict) -> bool:
    t = str(qd.get("time") or "")
    return t[:8] == now().strftime("%Y%m%d")


# ----------------------------------------------------------------------------- 分时
def intraday(code: str) -> dict:
    code = norm_code(code)
    if MOCK:
        return mock.intraday(code)

    def load():
        d = json.loads(http_get("https://web.ifzq.gtimg.cn/appstock/app/minute/query?code=" + code))
        node = ((d.get("data") or {}).get(code) or {})
        arr = (node.get("data") or {}).get("data") or []
        prev = None
        qt = (node.get("qt") or {}).get(code)
        if qt and len(qt) > 4:
            prev = _f(qt[4])
        pts, last_v = [], 0.0
        if not arr:
            return {}
        for s in arr:
            p = s.split(" ")
            if len(p) < 3:
                continue
            v = float(p[2]) * 100
            a = float(p[3]) if len(p) > 3 else 0.0
            pts.append({"t": p[0][:2] + ":" + p[0][2:], "p": float(p[1]), "v": max(v - last_v, 0),
                        "avg": (a / v) if v else float(p[1])})
            last_v = v
        return {"code": code, "prev": prev, "points": pts, "date": (node.get("data") or {}).get("date"), "source": "腾讯"}

    def load_ths():
        tc = ths_code(code)
        d = _jsonp(http_get(f"https://d.10jqka.com.cn/v6/time/hs_{tc}/last.js", "https://stockpage.10jqka.com.cn/",
                            retries=1))
        node = d.get(f"hs_{tc}") or {}
        pts = []
        for item in (node.get("data") or "").split(";"):
            p = item.split(",")
            if len(p) < 5:
                continue
            pts.append({"t": p[0][:2] + ":" + p[0][2:], "p": float(p[1]), "v": float(p[4]), "avg": float(p[3])})
        return {"code": code, "prev": _f(node.get("pre")), "points": pts, "date": node.get("date"), "source": "同花顺"}

    def both():
        rows, _ = _try_sources("分时 " + code, [("腾讯", load), ("同花顺", load_ths)])
        return rows
    return _cached("m:" + code, 20.0, both)


# ----------------------------------------------------------------------------- 主力资金（日）
def fundflow(code: str, n: int = 60) -> list[dict]:
    code = norm_code(code)
    if MOCK:
        return mock.fundflow(code, n)

    def load():
        secid = ("1." if code.startswith("sh") else "0.") + code[2:]
        url = ("https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get?lmt=%d&klt=101&secid=%s"
               "&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56,f57" % (n, secid))
        rows = []
        if time.time() > _em_state.get("ff_bad_until", 0):
            try:
                d = json.loads(http_get(url, "https://data.eastmoney.com/", retries=1))
                for s in ((d.get("data") or {}).get("klines") or []):
                    p = s.split(",")
                    rows.append({"code": code, "date": p[0], "main": _f(p[1], 0), "small": _f(p[2], 0),
                                 "mid": _f(p[3], 0), "large": _f(p[4], 0), "xlarge": _f(p[5], 0), "main_pct": _f(p[6], 0)})
                _stat("东财", True)
            except RuntimeError as e:
                _stat("东财", False, str(e))
                _em_state["ff_bad_until"] = time.time() + 600
        if not rows:  # 新浪资金流向（历史，按日）
            try:
                u2 = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/MoneyFlow.ssl_qsfx_zjlrqs"
                      f"?page=1&num={n}&sort=opendate&asc=0&daima={code}")
                arr = json.loads(http_get(u2, "https://vip.stock.finance.sina.com.cn/", retries=1) or b"[]") or []
                for a in arr:
                    main, xl = _f(a.get("netamount"), 0) or 0, _f(a.get("r0_net"), 0) or 0
                    rows.append({"code": code, "date": a["opendate"], "main": main, "small": None, "mid": None,
                                 "large": main - xl, "xlarge": xl, "main_pct": round((_f(a.get("ratioamount"), 0) or 0) * 100, 2)})
                rows.sort(key=lambda r: r["date"])
                _stat("新浪", True)
            except (RuntimeError, ValueError, KeyError) as e:
                _stat("新浪", False, str(e))
        if not rows:
            return db.q("SELECT * FROM fundflow WHERE code=? ORDER BY date DESC LIMIT ?", (code, n))[::-1]
        if rows:
            with db.tx() as c:
                c.executemany("INSERT OR REPLACE INTO fundflow VALUES(?,?,?,?,?,?,?,?)",
                              [(r["code"], r["date"], r["main"], r["small"], r["mid"], r["large"], r["xlarge"],
                                r["main_pct"]) for r in rows])
        return rows
    return _cached("ff:" + code, 60.0, load)


def search(text: str, limit: int = 12) -> list[dict]:
    t = text.strip()
    if not t:
        return []
    if re.fullmatch(r"(sh|sz)?\d{6}", t.lower()):
        c = norm_code(t)
        r = db.q1("SELECT code, name FROM universe WHERE code=?", (c,))
        if r:
            return [r]
        qd = quotes([c]).get(c)
        return [{"code": c, "name": qd["name"]}] if qd else []
    return db.q("SELECT code, name FROM universe WHERE name LIKE ? OR code LIKE ? LIMIT ?",
                (f"%{t}%", f"%{t}%", limit))


def market_phase(t: datetime | None = None) -> str:
    t = t or now()
    if t.weekday() >= 5:
        return "closed"
    hm = t.hour * 100 + t.minute
    if 915 <= hm < 925:
        return "auction"
    if 925 <= hm < 930:
        return "pre"
    if 930 <= hm < 1130 or 1300 <= hm < 1457:
        return "trading"
    if 1457 <= hm < 1500:
        return "close_auction"
    if 1130 <= hm < 1300:
        return "noon"
    if hm >= 1500:
        return "after"
    return "before"


def is_trading_day() -> bool:
    """用上证指数报价日期判断今天是否开市（自动识别节假日）。"""
    if now().weekday() >= 5:
        return False
    if MOCK:
        return True
    if now().hour < 9 or (now().hour == 9 and now().minute < 25):
        return True  # 开盘前无法判断，先按工作日处理
    ok = _cached("tday:" + today(), 600.0, _check_trading_day)
    if not ok:  # 判定为休市时不长期缓存，避免开盘瞬间数据未更新导致误判
        with _cache_lock:
            _cache.pop("tday:" + today(), None)
    return ok


def _check_trading_day() -> bool:
    try:
        qd = quotes(["sh000001"]).get("sh000001")
        if now().hour < 9 or (now().hour == 9 and now().minute < 25):
            return True  # 开盘前无法判断，先按工作日处理
        return bool(qd and is_trading_day_live(qd))
    except RuntimeError:
        return True


# ----------------------------------------------------------------------------- 模拟行情（离线）
def _h(s):
    import zlib
    return zlib.crc32(s.encode())


class _Mock:
    """可复现的随机行情，用于离线测试与演示。"""

    NAMES = "华光 东方 中信 申达 金桥 海天 长城 远航 宏达 恒信 天润 瑞丰 永兴 国泰 北辰 星河 嘉禾 盛世 鼎盛 汇丰".split()
    SUFFIX = "科技 电子 药业 能源 材料 股份 制造 智能 新材 控股".split()

    def __init__(self):
        rnd = random.Random(7)
        self.codes = []
        for i in range(320):
            code = ("sh60%04d" % (i * 7 + 1)) if i % 2 else ("sz00%04d" % (i * 5 + 1))
            name = rnd.choice(self.NAMES) + rnd.choice(self.SUFFIX) + ("" if i % 37 else "ST")
            self.codes.append((code, name))
        self.names = dict(self.codes)
        self._k = {}
        self.t0 = time.time()

    def kline(self, code, n=250):
        if code not in self._k:
            rnd = random.Random(_h(code) & 0xFFFFFF)
            p = {"sh000001": 3000.0, "sz399001": 9500.0, "sz399006": 2000.0, "sh000300": 3400.0,
                 "sh000016": 2500.0, "sh000688": 950.0}.get(code) or rnd.uniform(5, 80)
            drift = rnd.uniform(-0.002, 0.003)
            d = now().date() - timedelta(days=760)
            rows = []
            while d < now().date() - timedelta(days=1):
                d += timedelta(days=1)
                if d.weekday() >= 5:
                    continue
                o = p * (1 + rnd.gauss(0, 0.006))
                c = max(1.0, o * (1 + rnd.gauss(drift, 0.022)))
                h = max(o, c) * (1 + abs(rnd.gauss(0, 0.008)))
                lo = min(o, c) * (1 - abs(rnd.gauss(0, 0.008)))
                rows.append({"date": d.isoformat(), "open": round(o, 2), "close": round(c, 2), "high": round(h, 2),
                             "low": round(lo, 2), "vol": rnd.uniform(2e6, 3e7)})
                p = c
            self._k[code] = rows
        return self._k[code][-n:]

    def _live(self, code):
        k = self.kline(code, 30)
        last = k[-1]
        rnd = random.Random((_h(code) ^ int((time.time() - self.t0) / 3)) & 0xFFFFFF)
        base = random.Random(_h(code) & 0xFFFF).gauss(0.002, 0.025)
        pct = max(-0.099, min(0.099, base + rnd.gauss(0, 0.002)))
        price = round(last["close"] * (1 + pct), 2)
        avgv = sum(x["vol"] for x in k[-5:]) / 5
        vr = abs(random.Random(_h(code) & 0xFFF).gauss(1.1, 0.6)) + 0.2
        vol = avgv * vr * 0.8
        return {"code": code, "name": self.names.get(code, "模拟" + code[-4:]), "price": price,
                "prev": last["close"], "open": round(last["close"] * (1 + base / 3), 2),
                "high": round(max(price, last["close"]) * 1.01, 2), "low": round(min(price, last["close"]) * 0.99, 2),
                "pct": round(pct * 100, 2), "chg": round(price - last["close"], 2), "vol": vol,
                "amount": vol * price, "turnover": round(vr * 2.1, 2), "vr": round(vr, 2), "pe": 25.0, "pb": 2.1,
                "mcap": 80.0, "fmcap": 60.0, "amp": 3.0, "time": now().strftime("%Y%m%d%H%M%S"),
                "main_net": vol * price * base * 0.8, "main_pct": round(base * 100, 2), "listed": "2015-01-01",
                "bids": [[round(price - 0.01 * (i + 1), 2), 100 + i * 50] for i in range(5)],
                "asks": [[round(price + 0.01 * (i + 1), 2), 120 + i * 40] for i in range(5)],
                "avg": price * 0.998}

    def quotes(self, codes):
        out = {}
        for c in codes:
            if c.startswith(("hk", "us")) or c[2:5] in ("000", "399") and c in (
                    "sh000001", "sz399001", "sz399006", "sh000300", "sh000016", "sh000688"):
                rnd = random.Random(_h(c) & 0xFFFF)
                base = {"sh000001": 3658.0, "sz399001": 11357.0, "sz399006": 2410.0, "sh000300": 4036.0,
                        "sh000016": 2890.0, "sh000688": 1120.0}.get(c, 22934.0)
                pct = rnd.gauss(-0.3, 1.0)
                out[c] = {"code": c, "name": {"sh000001": "上证指数", "sz399001": "深证成指", "sz399006": "创业板指",
                                              "sh000300": "沪深300", "sh000016": "上证50", "sh000688": "科创50",
                                              "hkHSI": "恒生指数", "usIXIC": "纳斯达克", "usDJI": "道琼斯",
                                              "usINX": "标普500"}.get(c, c),
                          "price": round(base * (1 + pct / 100), 2), "prev": base, "pct": round(pct, 2),
                          "chg": round(base * pct / 100, 2), "amount": 4.2e11, "time": now().strftime("%Y%m%d%H%M%S")}
            else:
                out[c] = self._live(c)
        return out

    def snapshot(self):
        return [self._live(c) for c, _ in self.codes]

    def intraday(self, code):
        q = self._live(code) if not code.startswith(("sh000", "sz399")) else self.quotes([code])[code]
        rnd = random.Random(_h(code) & 0xFFFF)
        prev, p, pts = q["prev"], q["prev"], []
        times = [f"{h:02d}:{m:02d}" for h in range(9, 16) for m in range(60)
                 if (930 <= h * 100 + m <= 1130) or (1300 <= h * 100 + m <= 1500)]
        target = q["price"]
        for i, t in enumerate(times):
            p = p + (target - p) / max(1, len(times) - i) + rnd.gauss(0, prev * 0.0015)
            pts.append({"t": t, "p": round(p, 2), "v": rnd.uniform(1e4, 1e5), "avg": round((prev + p) / 2, 2)})
        return {"code": code, "prev": prev, "points": pts, "date": today()}

    def fundflow(self, code, n=60):
        rnd = random.Random(_h(code) & 0xFFFF)
        k = self.kline(code, n)
        return [{"code": code, "date": r["date"], "main": rnd.gauss(0, 3e7), "small": rnd.gauss(0, 1e7),
                 "mid": rnd.gauss(0, 1e7), "large": rnd.gauss(0, 2e7), "xlarge": rnd.gauss(0, 2e7),
                 "main_pct": rnd.gauss(0, 6)} for r in k]


mock = _Mock() if MOCK else None


def ensure_universe_mock():
    if MOCK:
        with db.tx() as c:
            c.executemany("INSERT OR REPLACE INTO universe(code,name,mcap,fmcap,updated) VALUES(?,?,?,?,?)",
                          [(cd, nm, 80, 60, today()) for cd, nm in mock.codes])


def settings_codes() -> tuple[list, list, list]:
    s = load_settings()
    return s["indices"], s["overseas"], s["watchlist"]


# ----------------------------------------------------------------------------- 数据源体检
PROBES = {
    "东财全市场快照": "https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=2&po=1&np=1&fltt=2&invt=2&fid=f3&fs=m:1+t:2&fields=f12,f14,f3,f100",
    "东财行业板块": "https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=2&po=1&np=1&fltt=2&invt=2&fid=f3&fs=m:90+t:2&fields=f12,f14,f3,f62,f104,f105,f109,f160,f128,f140,f136",
    "东财资金流": "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get?lmt=2&klt=101&secid=1.600519&fields1=f1,f2,f3&fields2=f51,f52,f53",
    "东财K线": "https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=1.600519&klt=101&fqt=1&lmt=2&end=20500101&fields1=f1&fields2=f51,f53",
    "腾讯行情": "https://qt.gtimg.cn/q=sh600519",
    "腾讯K线": "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh600519,day,,,2,qfq",
    "新浪行业板块": "https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php",
    "新浪概念板块": "https://money.finance.sina.com.cn/q/view/newFLJK.php?param=class",
    "东财新闻": "https://search-api-web.eastmoney.com/search/jsonp?cb=cb&param=%7B%22keyword%22%3A%22600519%22%2C%22type%22%3A%5B%22cmsArticleWebOld%22%5D%2C%22client%22%3A%22web%22%2C%22param%22%3A%7B%22cmsArticleWebOld%22%3A%7B%22pageIndex%22%3A1%2C%22pageSize%22%3A1%7D%7D%7D",
}


def probe(raw: bool = False, direct: bool | None = None, urls: list | None = None, n: int = 600) -> list[dict]:
    """逐个测试数据源能否连通（不走限速；每个最多 6 秒）。direct=True 绕过系统代理。"""
    out = []
    op = _DIRECT if direct else (_PROXY if direct is False else _opener_for)
    items = PROBES.items()
    if urls:
        items = [(u, u) for u in urls if (urllib.parse.urlsplit(u).hostname or "").endswith(CN_HOSTS)]
    for name, url in items:
        t0 = time.time()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": "https://quote.eastmoney.com/",
                                                       "Accept-Encoding": "gzip"})
            o = op(url) if callable(op) and op is _opener_for else op
            with o.open(req, timeout=6) as r:
                b = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    b = gzip.decompress(b)
            txt = (b[:n] if n > 0 else b[n:]).decode("gbk" if "sina" in url or "gtimg" in url else "utf-8", "ignore")
            out.append({"name": name, "ok": True, "ms": int((time.time() - t0) * 1000), "bytes": len(b),
                        "head": txt if raw else txt[:80]})
        except Exception as e:  # noqa: BLE001
            out.append({"name": name, "ok": False, "ms": int((time.time() - t0) * 1000), "error": str(e)[:160]})
    return out
