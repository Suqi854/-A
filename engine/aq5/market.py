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


def http_get(url: str, referer: str = "https://finance.qq.com", timeout: float = 8, retries: int = 2) -> bytes:
    last = None
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": referer,
                                                       "Accept-Encoding": "gzip"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return raw
        except Exception as e:  # noqa: BLE001 - 网络错误种类很多，统一重试
            last = e
            time.sleep(0.4 * (i + 1))
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
    d = json.loads(http_get(url, "https://quote.eastmoney.com/", retries=3))
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


def snapshot(force: bool = False) -> list[dict]:
    """沪深主板全市场实时快照（约3000+只）。东财为主，失败页由腾讯批量报价补齐。"""
    if MOCK:
        return mock.snapshot()

    def load():
        total, first = _em_page(1)
        rows = list(first)
        pages = max(1, (total + 99) // 100)
        failed = []

        def one(pn):
            try:
                return pn, _em_page(pn)[1]
            except RuntimeError:
                return pn, None
        for pn, part in ThreadPoolExecutor(max_workers=4).map(one, range(2, pages + 1)):
            if part is None:
                failed.append(pn)
            else:
                rows.extend(part)
        for pn in failed:  # 慢速重试一次
            time.sleep(0.8)
            try:
                rows.extend(_em_page(pn)[1])
            except RuntimeError:
                pass
        out = {r["code"]: r for r in (_em_row(x) for x in rows) if r}
        uni = {u["code"]: u for u in db.q("SELECT code, name FROM universe")}
        missing = [c for c in uni if c not in out]
        if missing:  # 东财缺页 -> 腾讯补
            for c, qd in quotes(missing).items():
                if is_main_board(c):
                    out[c] = {**qd, "main_net": None, "main_pct": None, "listed": None}
        if out:
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
def _fetch_kline(code: str, n: int) -> list[dict]:
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,{n},qfq"
    d = json.loads(http_get(url))
    node = (d.get("data") or {}).get(code) or {}
    arr = node.get("qfqday") or node.get("day") or []
    out = []
    for a in arr:
        if len(a) >= 6:
            out.append({"date": a[0], "open": float(a[1]), "close": float(a[2]), "high": float(a[3]),
                        "low": float(a[4]), "vol": float(a[5]) * 100})
    return out


def kline(code: str, n: int = 250, refresh: bool = False) -> list[dict]:
    code = norm_code(code)
    if MOCK:
        return mock.kline(code, n)
    meta = db.q1("SELECT updated, n FROM kline_meta WHERE code=?", (code,))
    t = now()
    fresh = False
    if meta and meta["n"] >= min(n, 120) and meta["updated"][:10] == today():
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
        for s in arr:
            p = s.split(" ")
            if len(p) < 3:
                continue
            v = float(p[2]) * 100
            a = float(p[3]) if len(p) > 3 else 0.0
            pts.append({"t": p[0][:2] + ":" + p[0][2:], "p": float(p[1]), "v": max(v - last_v, 0),
                        "avg": (a / v) if v else float(p[1])})
            last_v = v
        return {"code": code, "prev": prev, "points": pts, "date": (node.get("data") or {}).get("date")}
    return _cached("m:" + code, 20.0, load)


# ----------------------------------------------------------------------------- 主力资金（日）
def fundflow(code: str, n: int = 60) -> list[dict]:
    code = norm_code(code)
    if MOCK:
        return mock.fundflow(code, n)

    def load():
        secid = ("1." if code.startswith("sh") else "0.") + code[2:]
        url = ("https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get?lmt=%d&klt=101&secid=%s"
               "&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56,f57" % (n, secid))
        try:
            d = json.loads(http_get(url, "https://data.eastmoney.com/"))
        except RuntimeError:
            return db.q("SELECT * FROM fundflow WHERE code=? ORDER BY date DESC LIMIT ?", (code, n))[::-1]
        rows = []
        for s in ((d.get("data") or {}).get("klines") or []):
            p = s.split(",")
            rows.append({"code": code, "date": p[0], "main": _f(p[1], 0), "small": _f(p[2], 0),
                         "mid": _f(p[3], 0), "large": _f(p[4], 0), "xlarge": _f(p[5], 0), "main_pct": _f(p[6], 0)})
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
