"""大盘状态：识别放量 / 缩量 / 上涨 / 下跌 / 阴跌 / 异动拉升 / 异动跳水 / V形反转 / 冲高回落，并给个股分析和 AI 盘做参考。

公式：
  预估全天量比 = 当前累计成交量 ÷ 时间进度^0.85 ÷ 近5日平均成交量（A股成交量早盘集中，所以用 0.85 次方修正）
  放量：量比 ≥ 1.2；缩量：≤ 0.8
  5 分钟速度 = 最近 5 分钟涨跌幅；|速度| ≥ 0.4% 或 15 分钟 ≥ 0.8% 视为异动
  阴跌：近 5 日至少 3 天下跌、累计 ≤ −1.5%、每天跌幅都不大（< 1.5%）且成交量萎缩
  市场温度 = 50 + 12×沪深300涨幅 + 40×(上涨家数占比 − 0.5)，截断到 0~100
"""
from __future__ import annotations

import time

from . import db, market
from .config import now, today

INDICES = [("sh000001", "上证指数"), ("sz399001", "深证成指"), ("sh000300", "沪深300"), ("sz399006", "创业板指")]
STATE = {"label": None, "shifts": [], "since": None}
_cache = {"t": 0.0, "data": None}
_alerted: dict = {}  # (指数代码, 异动类型) -> 上次提醒时间，同类 30 分钟内不重复
IDX_ALERTS = ("异动拉升", "异动跳水", "V形反转", "冲高回落")

SCHEMA = """
CREATE TABLE IF NOT EXISTS market_daily(date TEXT PRIMARY KEY, label TEXT, pct REAL, vol_ratio REAL, up_ratio REAL,
  temperature REAL, morning TEXT, shifts TEXT);
CREATE TABLE IF NOT EXISTS forecasts(id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, kind TEXT, code TEXT, name TEXT,
  phase TEXT, predicted TEXT, actual REAL, correct INTEGER, note TEXT);
"""


def init() -> None:
    with db.tx() as c:
        c.executescript(SCHEMA)
    r = db.q1("SELECT shifts FROM market_daily WHERE date=?", (today(),))
    if r:
        STATE["shifts"] = db.loads(r["shifts"], [])


def _minutes_elapsed() -> int:
    t = now()
    m = t.hour * 60 + t.minute
    if m < 570:
        return 0
    if m <= 690:
        return m - 570
    if m < 780:
        return 120
    return min(240, 120 + m - 780)


def index_state(code: str, name: str) -> dict:
    rows = market.kline(code, 30)
    if rows and rows[-1]["date"] == today():
        hist = rows[:-1]
    else:
        hist = rows
    out = {"code": code, "name": name}
    if len(hist) < 10:
        return out
    closes = [r["close"] for r in hist]
    vols = [r["vol"] for r in hist]
    vma5 = sum(vols[-5:]) / 5
    d5 = [(closes[i] / closes[i - 1] - 1) * 100 for i in range(len(closes) - 5, len(closes))]
    yin = sum(1 for x in d5 if x < 0) >= 3 and sum(d5) <= -1.5 and all(abs(x) < 1.5 for x in d5) and \
        sum(vols[-5:]) < sum(vols[-10:-5])
    out.update(d5=[round(x, 2) for x in d5], sum5=round(sum(d5), 2), yin=yin)
    try:
        d = market.intraday(code)
    except RuntimeError:
        d = {}
    pts = d.get("points") or []
    prev = d.get("prev") or closes[-1]
    m = _minutes_elapsed()
    if not pts or not m or str(d.get("date") or "").replace("-", "")[:8] != now().strftime("%Y%m%d"):
        out.update(live=False, label="阴跌" if yin else "休市", pct=None)
        return out
    px = pts[-1]["p"]
    pct = (px / prev - 1) * 100
    cum = sum(p["v"] for p in pts)
    frac = (m / 240) ** 0.85
    vr = cum / frac / vma5 if vma5 and frac else None
    sp = lambda k: (px / pts[-1 - k]["p"] - 1) * 100 if len(pts) > k else 0.0  # noqa: E731
    s5, s15 = sp(5), sp(15)
    lo_i = min(range(len(pts)), key=lambda i: pts[i]["p"])
    hi_i = max(range(len(pts)), key=lambda i: pts[i]["p"])
    lo, hi = pts[lo_i]["p"], pts[hi_i]["p"]
    vol_tag = "放量" if vr and vr >= 1.2 else "缩量" if vr and vr <= 0.8 else "平量"
    dir_tag = "上涨" if pct >= 0.5 else "下跌" if pct <= -0.5 else "震荡"
    label = vol_tag + dir_tag
    events = []
    if s5 >= 0.4 or s15 >= 0.8:
        events.append("异动拉升")
    elif s5 <= -0.4 or s15 <= -0.8:
        events.append("异动跳水")
    if lo_i < len(pts) - 20 and (px / lo - 1) * 100 >= 1.0 and pct > 0 and lo < prev:
        events.append("V形反转")
    if hi_i < len(pts) - 20 and (1 - px / hi) * 100 >= 1.0 and hi > prev:
        events.append("冲高回落")
    if yin and pct < 0.3:
        events.append("阴跌")
    if events:
        label = events[0] if events[0] in ("异动拉升", "异动跳水") else label + "·" + "·".join(events)
    out.update(live=True, pct=round(pct, 2), vol_ratio=round(vr, 2) if vr else None, speed5=round(s5, 2),
               speed15=round(s15, 2), label=label, vol_tag=vol_tag, dir_tag=dir_tag, events=events,
               vwap=round(pts[-1].get("avg") or px, 2), above_vwap=px >= (pts[-1].get("avg") or px),
               low_time=pts[lo_i]["t"], high_time=pts[hi_i]["t"])
    return out


def state(force: bool = False) -> dict:
    if not force and _cache["data"] and time.time() - _cache["t"] < 60:
        return _cache["data"]
    idx = []
    for code, name in INDICES:
        try:
            idx.append(index_state(code, name))
        except Exception:  # noqa: BLE001
            idx.append({"code": code, "name": name})
    _alert_indices(idx)
    main = next((x for x in idx if x["code"] == "sh000300"), {})
    try:
        br = market.breadth(market.snapshot())
        up_ratio = br["up"] / br["total"] if br["total"] else 0.5
    except Exception:  # noqa: BLE001
        br, up_ratio = {}, 0.5
    pct300 = main.get("pct") or 0
    temp = max(0.0, min(100.0, 50 + 12 * pct300 + 40 * (up_ratio - 0.5)))
    label = main.get("label") or ("阴跌" if main.get("yin") else "休市")
    risk_off = ("异动跳水" in label) or (main.get("vol_tag") == "放量" and pct300 <= -1) or up_ratio < 0.15
    stance = "防守" if risk_off or temp < 30 else "谨慎" if temp < 45 or main.get("yin") else "顺势做多" if temp >= 60 else "中性"
    advice = {"防守": "大盘风险高：暂停新开仓，已有持仓收紧止损", "谨慎": "大盘偏弱：只做 A 级机会、仓位减半",
              "中性": "大盘中性：按计划执行，控制仓位", "顺势做多": "大盘偏强：可以积极一些，优先强势板块"}[stance]
    data = {"time": now().strftime("%H:%M:%S"), "indices": idx, "label": label, "temperature": round(temp, 1),
            "up_ratio": round(up_ratio, 3), "risk_off": risk_off, "stance": stance, "advice": advice,
            "breadth": {k: br.get(k) for k in ("up", "down", "limit_up", "limit_down", "amount_yi", "median_pct")},
            "shifts": STATE["shifts"][-12:]}
    if main.get("live"):
        _track_shift(label, data)
    _cache.update(t=time.time(), data=data)
    return data


def _alert_indices(idx: list[dict]) -> None:
    """指数层面的异动写入事件（kind=market），前端小喇叭读取；规则沿用上面的 5/15 分钟速度阈值。"""
    t = time.time()
    for x in idx:
        if not x.get("live"):
            continue
        for ev in x.get("events") or []:
            if ev not in IDX_ALERTS or t - _alerted.get((x["code"], ev), 0) < 1800:
                continue
            _alerted[(x["code"], ev)] = t
            db.event("market", f"{x['name']} {ev}", f"现 {x.get('pct', 0):+.2f}%，5分钟 {x.get('speed5', 0):+.2f}%，15分钟 {x.get('speed15', 0):+.2f}%",
                     "warn" if ev in ("异动跳水", "冲高回落") else "info")


def _track_shift(label: str, data: dict) -> None:
    """记录盘中状态切换（如 下跌 → V形反转）；切换时触发重新分析。"""
    base = label.split("·")[0]
    prev = STATE["label"]
    if prev is None:
        STATE.update(label=base, since=data["time"])
        return
    if base == prev:
        return
    shift = {"time": data["time"], "from": prev, "to": base, "temperature": data["temperature"]}
    STATE.update(label=base, since=data["time"])
    STATE["shifts"] = (STATE["shifts"] + [shift])[-30:]
    db.event("market", f"大盘状态变化：{prev} → {base}", f"市场温度 {data['temperature']}，{data['advice']}",
             "warn" if data["risk_off"] else "info")
    with db.tx() as c:
        c.execute("INSERT INTO market_daily(date, shifts) VALUES(?,?) ON CONFLICT(date) DO UPDATE SET shifts=excluded.shifts",
                  (today(), db.dumps(STATE["shifts"])))
    _on_shift(shift)


_last_rerun = {"t": 0.0}


def _on_shift(shift: dict) -> None:
    """大盘状态切换：重新跑自选信号；AI 简报盘中更新（每 40 分钟最多一次）。"""
    import threading
    if time.time() - _last_rerun["t"] < 600:
        return
    _last_rerun["t"] = time.time()

    def go():
        try:
            from . import signals
            signals.run_watch("intra")
        except Exception:  # noqa: BLE001
            pass
        try:
            from . import ai
            if ai.ready() and ai.cfg().get("auto_brief", True):
                ai.intraday_update(f"大盘从「{shift['from']}」变为「{shift['to']}」")
        except Exception:  # noqa: BLE001
            pass
    threading.Thread(target=go, daemon=True, name="aq5-shift").start()


def record_morning() -> None:
    """10:00 记下早盘判断，收盘后用来对照。"""
    s = state(force=True)
    with db.tx() as c:
        c.execute("INSERT INTO market_daily(date, morning) VALUES(?,?) ON CONFLICT(date) DO UPDATE SET morning=excluded.morning",
                  (today(), db.dumps({"label": s["label"], "temperature": s["temperature"], "stance": s["stance"],
                                      "pct300": next((x.get("pct") for x in s["indices"] if x["code"] == "sh000300"), None)})))


def close_review() -> dict:
    """收盘后：记录大盘全天状态；对照早盘判断和自选股盘前信号，统计对错。"""
    s = state(force=True)
    main = next((x for x in s["indices"] if x["code"] == "sh000300"), {})
    with db.tx() as c:
        c.execute("INSERT INTO market_daily(date, label, pct, vol_ratio, up_ratio, temperature, shifts) VALUES(?,?,?,?,?,?,?) "
                  "ON CONFLICT(date) DO UPDATE SET label=excluded.label, pct=excluded.pct, vol_ratio=excluded.vol_ratio, "
                  "up_ratio=excluded.up_ratio, temperature=excluded.temperature, shifts=excluded.shifts",
                  (today(), s["label"], main.get("pct"), main.get("vol_ratio"), s["up_ratio"], s["temperature"],
                   db.dumps(STATE["shifts"])))
        c.execute("DELETE FROM forecasts WHERE date=?", (today(),))
    rows = []
    md = db.q1("SELECT morning FROM market_daily WHERE date=?", (today(),))
    mo = db.loads(md["morning"], None) if md else None
    if mo and main.get("pct") is not None:
        pred = "涨" if mo["stance"] == "顺势做多" else "跌" if mo["stance"] in ("防守", "谨慎") else "平"
        act = main["pct"]
        ok = (pred == "涨" and act > 0) or (pred == "跌" and act < 0) or (pred == "平" and abs(act) < 0.6)
        rows.append(("market", "sh000300", "沪深300", "morning", f"{mo['label']}（{mo['stance']}）", act, int(ok),
                     f"早盘温度 {mo['temperature']}，收盘 {act:+.2f}%" + ("" if ok else f"；盘中变化：{'、'.join(x['from'] + '→' + x['to'] for x in STATE['shifts'][-4:]) or '无'}")))
    for r in db.q("SELECT code, data FROM watch_signals WHERE date=? AND phase='pre'", (today(),)):
        d = db.loads(r["data"], {})
        q = market.quotes([r["code"]]).get(r["code"]) or {}
        act = q.get("pct")
        if act is None:
            continue
        a = d.get("action", "")
        pred = "涨" if a in ("买入", "加仓", "持有") else "跌" if a in ("减仓", "卖出", "止损") else "平"
        ok = (pred == "涨" and act > 0) or (pred == "跌" and act < 0) or (pred == "平" and abs(act) < 2)
        rows.append(("watch", r["code"], d.get("name"), "pre", f"{a}（预期{pred}）", act, int(ok),
                     f"盘前评分 {d.get('score')}，收盘 {act:+.2f}%，大盘 {main.get('pct') or 0:+.2f}%"))
    with db.tx() as c:
        c.executemany("INSERT INTO forecasts(date, kind, code, name, phase, predicted, actual, correct, note) "
                      "VALUES(?,?,?,?,?,?,?,?,?)", [(today(), *x) for x in rows])
    return {"n": len(rows), "correct": sum(x[6] for x in rows)}


def accuracy(days: int = 30) -> dict:
    rows = db.q("SELECT * FROM forecasts WHERE date >= date(?, ?) ORDER BY id DESC", (today(), f"-{days} day"))
    by = {}
    for r in rows:
        k = "大盘早盘判断" if r["kind"] == "market" else "自选盘前信号"
        by.setdefault(k, []).append(r["correct"])
    return {"by": {k: {"n": len(v), "acc": round(sum(v) / len(v), 3)} for k, v in by.items()},
            "recent": rows[:40], "days": db.q("SELECT * FROM market_daily ORDER BY date DESC LIMIT 20")}


def brief_for_ai() -> dict:
    s = state()
    return {"大盘状态": s["label"], "市场温度": s["temperature"], "上涨占比": s["up_ratio"], "立场": s["stance"],
            "建议": s["advice"], "各指数": [f"{x['name']} {x.get('pct')}% {x.get('label', '')} 量比{x.get('vol_ratio')}" for x in s["indices"]],
            "今日状态变化": [f"{x['time'][:5]} {x['from']}→{x['to']}" for x in s["shifts"]]}
