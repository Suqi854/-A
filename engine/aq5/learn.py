"""自我迭代：
1. 每天收盘后回填选股样本的未来收益（1/3/5日）。
2. 用 Rank IC 评估已完成标签窗口的线上候选，生成不自动启用的权重建议。
3. 统计已链接的模拟成交和候选路径，生成影子权重建议。
4. 样本够多后训练逻辑回归模型，给出“上涨概率”（经过历史校验）。
所有版本都留档，可在“学习中心”看到每一次变化及原因。
"""
from __future__ import annotations

import math
import random

from . import db
from .config import now

MIN_IC_SAMPLES = 60
MIN_MODEL_SAMPLES = 200
LABEL_RET = 1.0
MODEL_PROTOCOL = "closed-live-v1"
PROB_LABEL = "选出参考价至随后第3个完整交易日收盘毛收益超过1%的概率"
HEURISTIC_LABEL = "经验参考分，未经校准，不是成交胜率"

_model_cache = {"ver": None, "m": None}


# ----------------------------------------------------------------------------- 工具
def _rank(x):
    idx = sorted(range(len(x)), key=lambda i: x[i])
    r = [0.0] * len(x)
    i = 0
    while i < len(idx):
        j = i
        while j + 1 < len(idx) and x[idx[j + 1]] == x[idx[i]]:
            j += 1
        for k in range(i, j + 1):
            r[idx[k]] = (i + j) / 2
        i = j + 1
    return r


def spearman(a, b):
    if len(a) < 5:
        return None
    ra, rb = _rank(a), _rank(b)
    ma, mb = sum(ra) / len(ra), sum(rb) / len(rb)
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    den = math.sqrt(sum((x - ma) ** 2 for x in ra) * sum((y - mb) ** 2 for y in rb))
    return num / den if den else None


def _sig(z):
    z = max(-30, min(30, z))
    return 1 / (1 + math.exp(-z))


# ----------------------------------------------------------------------------- 回填未来收益
def _closed_rows(code, cutoff):
    from . import market
    try:
        rows = market.kline(code, 120, history=True)
    except RuntimeError:
        return []
    # 盘中 K 线可能包含半根今日数据。缓存也可能只有最近一个交易日的半根。
    cutoff_day = cutoff.strftime("%Y-%m-%d")
    allow_today = cutoff.hour * 100 + cutoff.minute >= 1505
    if not market.MOCK:
        meta = db.q1("SELECT updated FROM kline_meta WHERE code=?", (code,))
        if not meta:
            return []
        fetched_at = meta["updated"]
    else:
        fetched_at = cutoff.strftime("%Y-%m-%d %H:%M")
    return [r for r in rows if (r["date"] < cutoff_day or allow_today and r["date"] == cutoff_day)
            and fetched_at >= r["date"] + " 15:05" and (r.get("close") or 0) > 0
            and math.isfinite(r["close"])]


def fill_outcomes() -> dict:
    from . import research
    from .forecast.calendar import calendar_window
    cutoff = now()
    cut_date = cutoff.strftime("%Y-%m-%d")
    has_research = research.has_schema()
    join = (" LEFT JOIN research_samples rs ON rs.run_id=p.run_id AND rs.code=p.code "
            if has_research else "")
    features = "rs.factors" if has_research else "NULL"
    pend = []
    for table, kind, extra in (("samples", "sample", ""), ("picks", "pick", ",p.stop,p.target1")):
        pend.extend(db.q(f"SELECT p.id,p.code,p.date,p.price,{features} AS features,? AS t{extra} "
                         f"FROM {table} p {join} WHERE p.evaluated=0 AND p.date<?", (kind, cut_date)))
    if not pend:
        return {"updated": 0}
    days = sorted({r["date"] for r in _closed_rows("sh000300", cutoff)})
    if not days:
        return {"updated": 0, "reason": "没有可核实的已完成交易日"}
    research.init()
    done, cache = 0, {}
    closed_dates = set(days)
    calendars = {}
    futures = {}
    for p in pend:
        if p["date"] not in closed_dates or not p["price"] or not math.isfinite(p["price"]):
            continue
        evidence = db.loads(p["features"], {})
        if "reference" in evidence and not evidence["reference"]:
            continue
        if p["code"] not in cache:
            cache[p["code"]] = {r["date"]: r for r in _closed_rows(p["code"], cutoff)}
        key = (p["date"], "reference" in evidence)
        if key not in futures:
            calendar = days
            if key[1]:
                year = int(p["date"][:4])
                if year not in calendars:
                    calendars[year] = calendar_window(year)
                calendar = calendars[year] if p["date"] in calendars[year] else []
            futures[key] = [d for d in calendar if d > p["date"]][:5]
        path = []
        for day in futures[key]:
            row = cache[p["code"]].get(day)
            if day not in closed_dates or not row:
                break  # 缺前一根时，不拿后面的收盘价代替
            path.append(row)
        if not path:
            continue
        scale = 1.0
        if ref := evidence.get("reference"):
            # 市场K线可能已前复权。用信号日同一开盘价把未来K线统一为冻结报价的尺度。
            adj_open = (cache[p["code"]].get(p["date"]) or {}).get("open")
            if not adj_open or not math.isfinite(adj_open) or adj_open <= 0:
                continue
            scale = adj_open / ref["open"]
        base = p["price"] * scale

        def ret(k):
            return round((path[k - 1]["close"] / base - 1) * 100, 3) if len(path) >= k else None

        r1, r3, r5 = ret(1), ret(3), ret(5)
        complete = r5 is not None
        with db.tx() as c:
            if p["t"] == "sample":
                c.execute("""UPDATE samples SET ret_1d=?, ret_3d=COALESCE(?, ret_3d),
                          ret_5d=COALESCE(?, ret_5d), evaluated=? WHERE id=? AND evaluated=0""",
                          (r1, r3, r5, int(complete), p["id"]))
                if complete:
                    c.execute("INSERT OR IGNORE INTO research_label_windows VALUES(?,?,?)",
                              (p["id"], path[2]["date"], path[4]["date"]))
            else:
                mu = round((max((r.get("high") or r["close"]) for r in path) / base - 1) * 100, 2)
                md = round((min((r.get("low") or r["close"]) for r in path) / base - 1) * 100, 2)
                outcome = "持有中"
                for r in path:
                    if p["stop"] and (r.get("low") or r["close"]) <= p["stop"] * scale:
                        outcome = "触发止损"
                        break
                    if p["target1"] and (r.get("high") or r["close"]) >= p["target1"] * scale:
                        outcome = "达到止盈1"
                        break
                else:
                    if complete:
                        outcome = "5日上涨" if r5 > 0 else "5日下跌"
                c.execute("""UPDATE picks SET ret_1d=?, ret_3d=COALESCE(?, ret_3d), ret_5d=COALESCE(?, ret_5d),
                          max_up_5d=?, max_dn_5d=?, outcome=?, evaluated=? WHERE id=? AND evaluated=0""",
                          (r1, r3, r5, mu, md, outcome, int(complete), p["id"]))
        done += 1
    return {"updated": done}


# ----------------------------------------------------------------------------- 权重迭代
HALF_LIFE = 15        # 样本按日期衰减：15 天前的样本权重减半，近期表现说话更多
D1_WEIGHT = 0.6       # 只有 1 日收益（3 日还没出来）的样本组也参与，权重打六折：第二天就能反馈
PRIOR_N = 4           # 策略胜率的贝叶斯先验（相当于 4 个 50% 胜率的虚拟样本）：样本少也能动，但不会被一两次结果带偏


def _age_w(d: str, ref: str) -> float:
    from datetime import date
    try:
        age = (date.fromisoformat(ref) - date.fromisoformat(d[:10])).days
    except ValueError:
        return 0.5
    return 0.5 ** (max(0, age) / HALF_LIFE)


def _label(r) -> tuple[float | None, float]:
    """优先用 3 日收益；3 日还没出来时用 1 日收益（权重打折）。"""
    if r.get("ret_3d") is not None:
        return r["ret_3d"], 1.0
    if r.get("ret_1d") is not None:
        return r["ret_1d"], D1_WEIGHT
    return None, 0.0


def _strat_stat(hits: list[tuple[float, float]]) -> dict:
    """hits: [(收益%, 权重)] → 收缩后的胜率、均值和策略权重。"""
    n = sum(w for _, w in hits)
    if not hits or n <= 0:
        return {"n": 0, "n_eff": 0.0, "win": None, "avg": None, "weight": 1.0}
    win = sum(w for x, w in hits if x > 0) / n
    avg = sum(x * w for x, w in hits) / n
    win_post = (win * n + 0.5 * PRIOR_N) / (n + PRIOR_N)
    avg_post = avg * n / (n + PRIOR_N + 2)
    return {"n": len(hits), "n_eff": round(n, 1), "win": round(win, 3), "avg": round(avg, 3),
            "weight": round(max(0.5, min(1.5, 0.5 + win_post + avg_post / 5)), 3)}


def _realized() -> list[dict]:
    """AI 自动盘 / 计划模拟盘的真实平仓：按买入时选股记录里的策略归因，实战结果权重加倍。"""
    rows = db.q("SELECT t.time, t.pnl, t.qty, t.price, t.fee, p.strategies, p.session FROM trades t "
                "JOIN picks p ON p.id=t.pick_id AND p.code=t.code "
                "WHERE t.side='sell' AND t.pnl IS NOT NULL AND t.fee IS NOT NULL AND t.account IN ('ai','plan') "
                "ORDER BY t.id DESC LIMIT 2000")
    out = []
    for r in rows:
        basis = (r["qty"] or 0) * (r["price"] or 0) - r["fee"] - r["pnl"]
        if basis <= 0:
            continue
        ids = [x["id"] if isinstance(x, dict) else x for x in (db.loads(r["strategies"], []) or [])]
        out.append({"date": r["time"][:10], "ret": r["pnl"] / basis * 100, "ids": ids, "session": r["session"]})
    return out


def iterate(force: bool = False) -> dict:
    from .factors import DEFAULT_WEIGHTS, KEYS, current_weights
    from .strategies import STRATEGIES
    ref = now().strftime("%Y-%m-%d")
    rows = _mature_live()
    if not rows or (len(rows) < MIN_IC_SAMPLES and not force):
        return {"changed": False, "reason": f"有效样本 {len(rows)} 个，少于 {MIN_IC_SAMPLES}，权重保持不变"}
    groups = {}
    for r in rows:
        groups.setdefault((r["date"], r["session"]), []).append(r)
    ic = {k: [] for k in KEYS}   # [(ic, 权重)]
    for (d, _), g in groups.items():
        lab = []
        for x in g:
            y, hw = _label(x)
            if y is not None:
                lab.append((y, hw, x))
        if len(lab) < 8:
            continue
        gw = _age_w(d, ref) * min(t[1] for t in lab)
        y = [t[0] for t in lab]
        fs = [db.loads(t[2]["factors"], {}) for t in lab]
        for k in KEYS:
            v = spearman([f.get(k, 0) for f in fs], y)
            if v is not None:
                ic[k].append((v, gw))
    ic_mean, ic_ir = {}, {}
    for k, v in ic.items():
        W = sum(w for _, w in v)
        m = sum(x * w for x, w in v) / W if W else 0.0
        ic_mean[k] = m
        if len(v) > 2 and W:
            sd = math.sqrt(sum(w * (x - m) ** 2 for x, w in v) / W) or 1e-9
            ic_ir[k] = m / sd
        else:
            ic_ir[k] = 0.0
    ver, old = current_weights()
    target = {k: DEFAULT_WEIGHTS[k] * max(0.3, min(2.0, 1 + 4 * ic_mean[k])) for k in KEYS}
    tsum = sum(target.values())
    target = {k: v / tsum for k, v in target.items()}
    # 自适应步长：IC 越稳定（|IR| 大）的因子调得越快，最多一次走 50%；不稳定的只走 20%
    alpha = {k: max(0.2, min(0.5, 0.2 + 0.3 * min(1.0, abs(ic_ir[k])))) for k in KEYS}
    new = {k: (1 - alpha[k]) * old[k] + alpha[k] * target[k] for k in KEYS}
    new = {k: min(0.30, max(0.02, v)) for k, v in new.items()}
    s = sum(new.values())
    new = {k: round(v / s, 4) for k, v in new.items()}

    real = _realized()

    def stat_for(rs, rl, key):
        hits = []
        for r in rs:
            if key in (db.loads(r["strategies"], []) or []):
                y, hw = _label(r)
                if y is not None:
                    hits.append((y, hw * _age_w(r["date"], ref)))
        hits += [(x["ret"], 2.0 * _age_w(x["date"], ref)) for x in rl if key in x["ids"]]
        return _strat_stat(hits)

    strat = {}
    for key, name, *_ in STRATEGIES:
        strat[key] = {"name": name, **stat_for(rows, real, key)}
    # 分场次（竞价 / 尾盘 / 手动）各自统计策略表现：早盘和尾盘的策略分开迭代
    by_session = {}
    for sess in ("auction", "close", "manual"):
        rs = [r for r in rows if r["session"] == sess]
        rl = [x for x in real if x["session"] == sess]
        d = {key: stat_for(rs, rl, key) for key, *_ in STRATEGIES}
        by_session[sess] = {k: v for k, v in d.items() if v["n"]}
    strat["_by_session"] = by_session
    strat["_meta"] = {"realized": len(real), "half_life": HALF_LIFE, "alpha": {k: round(v, 2) for k, v in alpha.items()}}
    moved = sorted(KEYS, key=lambda k: -abs(new[k] - old[k]))[:3]
    note = "；".join(f"{k} {old[k]:.3f}→{new[k]:.3f}（IC {ic_mean[k]:+.3f}）" for k in moved)
    if real:
        note += f"；实战平仓 {len(real)} 笔参与策略评估"
    return {"changed": False, "shadow": True, "baseline_version": ver, "weights": new,
            "ic": ic_mean, "strat": strat, "note": "影子建议（未启用）： " + note}


def tune_hf(trading_day: str | None = None) -> dict:
    """Use reconciled simulation fills from the preceding exchange session for one day."""
    from .forecast.calendar import official_days
    clock = now()
    day = trading_day or clock.date().isoformat()
    year = int(day[:4])
    days = official_days(year)
    if trading_day is None and (day not in days or clock.hour >= 15):
        following = next((d for d in days if d > day), None)
        if not following:
            following = next(iter(official_days(year + 1)), None)
        if following:
            day = following
            days = official_days(int(day[:4]))
        else:
            days = []
    result = {"changed": False, "active": False, "multiplier": 1.0, "trading_day": day, "source_day": None,
              "source": "ai 模拟成交与持仓核对，含开平双边手续费，成交价已含滑点",
              "closes": 0, "matched": 0, "net": None, "win_rate": None}
    if day not in days:
        return {**result, "reason": "无已确认交易日历"}
    i = days.index(day)
    previous = days[i - 1] if i else next(reversed(official_days(int(day[:4]) - 1)), None)
    if not previous:
        return {**result, "reason": "缺上一交易日历"}
    result["source_day"] = previous
    # Reverse all subsequent signed fills from current inventory in the same
    # SQLite snapshot. Only a flat -> full position -> flat round trip qualifies.
    rows = db.q("""
        WITH inventory AS (
          SELECT t.*, COALESCE(p.qty,0) - COALESCE(SUM(CASE t.side WHEN 'buy' THEN t.qty ELSE -t.qty END)
              OVER(PARTITION BY t.code ORDER BY t.id ROWS BETWEEN 1 FOLLOWING AND UNBOUNDED FOLLOWING),0) AS after_qty
          FROM trades t LEFT JOIN positions p ON p.account=t.account AND p.code=t.code
          WHERE t.account='ai' AND t.time>=?
        )
        SELECT code,side,qty,price,fee,pnl,reason,after_qty FROM inventory WHERE time LIKE ? ORDER BY id
        """, (previous, previous + "%"))
    entries, nets = {}, []
    for r in rows:
        entry = entries.pop(r["code"], None)
        reason = r["reason"] or ""
        closing = reason.startswith(("高频·平多", "高频·平空"))
        result["closes"] += int(closing)
        if not isinstance(r["qty"], int) or r["qty"] <= 0:
            continue
        long_entry = reason.startswith("高频·做多") and r["side"] == "buy" and r["after_qty"] == r["qty"]
        short_entry = reason.startswith("高频·融券做空") and r["side"] == "sell" and r["after_qty"] == -r["qty"]
        if (long_entry or short_entry) and r["pnl"] is None:
            entries[r["code"]] = r
            continue
        if not closing or not entry or r["after_qty"] != 0 or r["qty"] != entry["qty"]:
            continue
        long = entry["side"] == "buy"
        if r["side"] == entry["side"] or not reason.startswith("高频·平多" if long else "高频·平空"):
            continue
        values = (entry["price"], r["price"], entry["fee"], r["fee"], r["pnl"])
        if not all(isinstance(v, (float, int)) and math.isfinite(v) for v in values):
            continue
        if min(values[:2]) <= 0 or min(values[2:4]) < 0:
            continue
        gross = (r["price"] - entry["price"]) * r["qty"] * (1 if long else -1)
        if abs(gross - r["fee"] - r["pnl"]) <= .011:
            nets.append(round(gross - r["fee"] - entry["fee"], 2))
    net = round(sum(nets), 2) if nets else None
    result.update(matched=len(nets), net=net, win_rate=sum(v > 0 for v in nets) / len(nets) if nets else None)
    if len(nets) < 15:
        return {**result, "reason": "可核对的日内往返少于15笔"}
    if net >= 0:
        return {**result, "reason": "可核对往返的费用后合计未亏损"}
    return {**result, "active": True, "multiplier": 1.1, "reason": "上一交易日已核对往返净亏，本交易日收紧高频开仓"}


# ----------------------------------------------------------------------------- 逻辑回归模型
def _features(raw: dict, sigs) -> list[float]:
    from .factors import KEYS
    from .strategies import STRATEGIES
    ids = set(s["id"] if isinstance(s, dict) else s for s in (sigs or []))
    values = [raw.get(k) for k in KEYS]
    return [float(v) if isinstance(v, (float, int)) and math.isfinite(v) else 0.0 for v in values] + [
        1.0 if key in ids else 0.0 for key, *_ in STRATEGIES]


def _mature_live() -> list[dict]:
    from . import research
    if not research.has_schema():
        return []
    return db.q("""SELECT s.id, s.date, s.session, s.score, s.factors, s.strategies, s.ret_1d, s.ret_3d, w.end_5d
                   FROM samples s JOIN research_runs r ON r.run_id=s.run_id
                   JOIN research_label_windows w ON w.sample_id=s.id
                   WHERE s.session IN ('auction','close') AND s.evaluated=1 AND s.ret_3d IS NOT NULL
                     AND r.version=? AND w.end_3d > s.date AND w.end_5d > w.end_3d AND w.end_5d <= ?
                   ORDER BY s.date DESC, s.id DESC LIMIT 8000""", (research.VERSION, now().strftime("%Y-%m-%d")))


def _holdout(rows: list[dict]) -> tuple[list, list]:
    dates = sorted({r["date"] for r in rows})
    cut = max(15, int(len(dates) * 0.8))
    if len(dates) - cut < 5:
        return [], []
    split = dates[cut]
    train_rows = [r for r in rows if r["date"] < split and r["end_5d"] < split]
    if len({r["date"] for r in train_rows}) < 10:
        return [], []
    return train_rows, [r for r in rows if r["date"] >= split]


def train() -> dict:
    rows = _mature_live()[::-1]
    if len(rows) < MIN_MODEL_SAMPLES:
        return {"trained": False, "reason": f"新协议下完成5日窗口的线上样本 {len(rows)}，至少需要 {MIN_MODEL_SAMPLES} 个"}
    train_rows, test_rows = _holdout(rows)
    if not train_rows:
        return {"trained": False, "reason": "独立交易日留出集不足，或排除5日标签重叠后训练窗口不足"}
    rows = train_rows + test_rows
    X = [_features(db.loads(r["factors"], {}), db.loads(r["strategies"], [])) for r in rows]
    y = [float(r["ret_3d"] > LABEL_RET) for r in rows]
    cut = len(train_rows)
    if cut < 100 or len(rows) - cut < 50 or len(set(y[:cut])) < 2 or len(set(y[cut:])) < 2:
        return {"trained": False, "reason": "清洗后训练或留出样本不足，或标签只有一类"}
    d = len(X[0])
    mu = [sum(x[j] for x in X[:cut]) / cut for j in range(d)]
    sd = [math.sqrt(sum((x[j] - mu[j]) ** 2 for x in X[:cut]) / cut) or 1.0 for j in range(d)]
    Z = [[(x[j] - mu[j]) / sd[j] for j in range(d)] for x in X]
    w, b, lr, lam = [0.0] * d, 0.0, 0.1, 0.01
    train_X = Z[:cut]
    for _ in range(250):
        gw, gb = [0.0] * d, 0.0
        for i, x in enumerate(train_X):
            e = _sig(sum(wj * xj for wj, xj in zip(w, x)) + b) - y[i]
            for j in range(d):
                gw[j] += e * x[j]
            gb += e
        w = [w[j] - lr * (gw[j] / cut + lam * w[j]) for j in range(d)]
        b -= lr * gb / cut
    ps = [_sig(sum(wj * xj for wj, xj in zip(w, x)) + b) for x in Z[cut:]]
    labels = y[cut:]
    pos = int(sum(labels))
    neg = len(labels) - pos
    auc = (sum(rank for rank, label in zip(_rank(ps), labels) if label) - pos * (pos - 1) / 2) / (pos * neg)
    n_top = max(5, len(labels) // 5)
    top = sorted(range(len(labels)), key=lambda i: -ps[i])[:n_top]
    prior = sum(y[:cut]) / cut
    brier = sum((p - t) ** 2 for p, t in zip(ps, labels)) / len(labels)
    base_brier = sum((prior - t) ** 2 for t in labels) / len(labels)
    usable = auc >= 0.55 and brier < base_brier
    metrics = {"protocol": MODEL_PROTOCOL, "label": PROB_LABEL, "usable": usable,
               "n": len(rows), "train": cut, "test": len(labels), "base_rate": round(prior, 3),
               "test_auc": round(auc, 3), "top20_hit": round(sum(labels[i] for i in top) / len(top), 3),
               "train_end": max(r["date"] for r in train_rows), "train_label_end": max(r["end_5d"] for r in train_rows),
               "test_start": min(r["date"] for r in test_rows), "test_end": max(r["date"] for r in test_rows),
               "brier": round(brier, 5), "prior_brier": round(base_brier, 5)}
    with db.tx() as c:
        c.execute("INSERT INTO models(created, coef, metrics) VALUES(?,?,?)",
                  (now().strftime("%Y-%m-%d %H:%M"), db.dumps({"w": w, "b": b, "mu": mu, "sd": sd}), db.dumps(metrics)))
    db.event("learn", "概率模型已验证", f"样本外 AUC {metrics['test_auc']}，{'可用于展示' if usable else '保留为未通过版本'}")
    return {"trained": True, "metrics": metrics}


def _model_rows() -> list[dict]:
    rows = db.q("SELECT version, created, coef, metrics FROM models ORDER BY version DESC")
    for r in rows:
        r["metrics"] = db.loads(r["metrics"], {})
    return [r for r in rows if r["metrics"].get("protocol") == MODEL_PROTOCOL and r["metrics"].get("usable")]


def _selected_model() -> dict | None:
    from . import research
    if not research.has_schema():
        return None
    version = db.q1("SELECT value FROM research_controls WHERE name='model_version'")
    return db.q1("""SELECT version, coef FROM models
                    WHERE CASE WHEN json_valid(metrics)
                               THEN json_extract(metrics, '$.protocol')=? AND json_extract(metrics, '$.usable')=1 END
                      AND (? IS NULL OR version=?)
                    ORDER BY version DESC LIMIT 1""",
                 (MODEL_PROTOCOL, version["value"] if version else None, version["value"] if version else None))


def select_model(version: int | None) -> dict:
    """显式回退到合格旧版本。0 使用经验分，None 恢复选择最新合格版本。"""
    from . import research
    if version not in (None, 0) and not any(r["version"] == version for r in _model_rows()):
        raise ValueError("所选模型版本未通过本协议验证")
    research.init()
    with db.tx() as c:
        if version is None:
            c.execute("DELETE FROM research_controls WHERE name='model_version'")
        else:
            c.execute("INSERT OR REPLACE INTO research_controls VALUES('model_version', ?)", (str(version),))
    return {"version": version}


def predict(raw: dict, sigs, total: float) -> tuple[float, str]:
    row = _selected_model()
    if row:
        if _model_cache["ver"] != row["version"]:
            _model_cache.update(ver=row["version"], m=db.loads(row["coef"], None))
        m = _model_cache["m"]
        x = _features(raw, sigs)
        if m and len(m["w"]) == len(x):
            z = sum(w * (v - mu) / sd for w, v, mu, sd in zip(m["w"], x, m["mu"], m["sd"])) + m["b"]
            return round(_sig(z), 3), "model"
    return round(0.2 + 0.5 * _sig((total - 65) / 8), 3), "heuristic"


BOOT = {"running": False, "progress": 0, "total": 0, "msg": ""}


def bootstrap(n_stocks: int = 200, days: int = 80, step: int = 2) -> dict:
    """历史冷启动：在历史K线上“回到过去”逐日计算因子，并用之后3日的真实涨跌做标签。
    这样不用等几周实盘，第一天就能做因子IC评估和模型训练。"""
    from concurrent.futures import ThreadPoolExecutor

    from . import market
    from .factors import raw_factors
    from .indicators import pack  # noqa: F401  (raw_factors 内部使用)
    from .strategies import detect
    if BOOT["running"]:
        return {"error": "冷启动正在进行中"}
    BOOT.update(running=True, progress=0, msg="选取样本股票")
    try:
        uni = [u["code"] for u in db.q("SELECT code, name FROM universe") if "ST" not in (u["name"] or "").upper()]
        if not uni:
            try:
                market.snapshot(force=True)
            except RuntimeError:
                pass
            uni = [u["code"] for u in db.q("SELECT code, name FROM universe") if "ST" not in (u["name"] or "").upper()]
        rnd = random.Random(now().strftime("%Y%m%d"))
        codes = rnd.sample(uni, min(n_stocks, len(uni)))
        BOOT.update(total=len(codes), msg="下载历史K线并回放")
        out = []

        def work(code):
            try:
                rows = market.kline(code, days + 140, history=True)
            except RuntimeError:
                return []
            res = []
            if len(rows) < 140:
                return res
            for t in range(len(rows) - days, len(rows) - 3, step):
                hist = rows[max(0, t - 130):t + 1]
                try:
                    raw, ind = raw_factors(hist, None)
                    sig = [s["id"] for s in detect(hist, ind, None, "close", {})]
                except (ZeroDivisionError, ValueError, TypeError, IndexError):
                    continue
                c0 = rows[t]["close"]
                r = [round((rows[t + k]["close"] / c0 - 1) * 100, 3) for k in (1, 3)]
                r5 = round((rows[t + 5]["close"] / c0 - 1) * 100, 3) if t + 5 < len(rows) else None
                res.append((rows[t]["date"], code, c0, raw, sig, r[0], r[1], r5))
            return res
        with ThreadPoolExecutor(max_workers=2) as ex:
            for part in ex.map(work, codes):
                BOOT["progress"] += 1
                out.extend(part)
        with db.tx() as c:
            c.execute("DELETE FROM samples WHERE session='history'")
            c.executemany("INSERT INTO samples(run_id, date, session, code, price, score, factors, strategies, ret_1d, "
                          "ret_3d, ret_5d, evaluated) VALUES(0,?,?,?,?,NULL,?,?,?,?,?,1)",
                          [(d, "history", code, p, db.dumps({k: round(v, 4) for k, v in raw.items()}), db.dumps(sig),
                            r1, r3, r5) for d, code, p, raw, sig, r1, r3, r5 in out])
        BOOT["msg"] = f"已生成 {len(out)} 个历史样本，开始评估"
        it = {"changed": False, "reason": "历史回放与线上迭代分开"}
        tr = {"trained": False, "reason": "历史回放不参与线上模型训练"}
        db.event("learn", "历史冷启动完成", f"{len(codes)} 只股票、{len(out)} 个样本；"
                 + (it.get("note") or it.get("reason", "")))
        BOOT["msg"] = f"完成：{len(out)} 个样本"
        return {"samples": len(out), "iterate": it, "model": tr}
    finally:
        BOOT["running"] = False


def daily() -> dict:
    from . import pick_policy
    a = fill_outcomes()
    admission = pick_policy.review()
    b = iterate()
    c = train()
    try:
        h = tune_hf()
    except Exception as e:  # noqa: BLE001
        h = {"error": str(e)}
    return {"outcomes": a, "iterate": b, "model": c, "hf": h, "pick_policy": admission}


def quick() -> dict:
    """午间只补齐已完成的交易日，生成影子权重建议。"""
    return {"outcomes": fill_outcomes(), "iterate": iterate(),
            "model": {"trained": False, "reason": "午间不重训"}}


def summary() -> dict:
    from . import pick_policy
    from .factors import LABELS, current_weights
    ver, w = current_weights()
    hist = db.q("SELECT version, created, weights, ic, strat, note FROM weights ORDER BY version DESC LIMIT 30")
    for h in hist:
        h["weights"] = db.loads(h["weights"], {})
        h["ic"] = db.loads(h["ic"], {})
        h["strat"] = db.loads(h["strat"], {})
    m = db.q1("SELECT version, created, metrics FROM models ORDER BY version DESC LIMIT 1")
    if m:
        m["metrics"] = db.loads(m["metrics"], {})
    n_s = db.q1("SELECT COUNT(*) n, SUM(ret_3d IS NOT NULL) e FROM samples")
    pk = db.q("SELECT ret_1d, ret_3d, ret_5d, outcome FROM picks WHERE ret_1d IS NOT NULL")

    def stat(key):
        v = [p[key] for p in pk if p[key] is not None]
        return {"n": len(v), "win": round(sum(1 for x in v if x > 0) / len(v), 3) if v else None,
                "avg": round(sum(v) / len(v), 2) if v else None}
    active_model = _selected_model()
    return {"version": ver, "weights": w, "labels": LABELS, "history": hist, "model": m,
            "pick_policy": {session: pick_policy.current(session) for session in pick_policy.SESSIONS},
            "model_protocol": MODEL_PROTOCOL, "prob_label": PROB_LABEL,
            "active_model_version": active_model["version"] if active_model else None,
            "samples": n_s["n"] or 0, "evaluated": n_s["e"] or 0,
            "picks": {"d1": stat("ret_1d"), "d3": stat("ret_3d"), "d5": stat("ret_5d"),
                      "outcomes": {o: sum(1 for p in pk if p["outcome"] == o) for o in
                                   ("达到止盈1", "触发止损", "5日上涨", "5日下跌", "持有中")}},
            "min_ic": MIN_IC_SAMPLES, "min_model": MIN_MODEL_SAMPLES}
