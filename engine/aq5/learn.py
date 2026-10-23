"""自我迭代：
1. 每天收盘后回填选股样本的未来收益（1/3/5日）。
2. 用 Rank IC 衡量每个因子最近的预测力，平滑地调整因子权重（有上下限，防止过拟合）。
3. 统计每个策略的实战胜率，调整策略权重。
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
LABEL_RET = 1.0  # 3日收益 > 1% 记为“成功”

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
def fill_outcomes() -> dict:
    from . import market
    pend = db.q("SELECT id, code, date, price, 'sample' AS t FROM samples WHERE evaluated=0 AND date<?",
                (now().strftime("%Y-%m-%d"),))
    pend += db.q("SELECT id, code, date, price, stop, target1, 'pick' AS t FROM picks WHERE evaluated=0 AND date<?",
                 (now().strftime("%Y-%m-%d"),))
    done = 0
    cache = {}
    for p in pend:
        if p["code"] not in cache:
            try:
                cache[p["code"]] = market.kline(p["code"], 120, history=True)
            except RuntimeError:
                cache[p["code"]] = []
        rows = [r for r in cache[p["code"]] if r["date"] > p["date"]]
        if not rows or not p["price"]:
            continue
        base = p["price"]

        def ret(k):
            return round((rows[k - 1]["close"] / base - 1) * 100, 3) if len(rows) >= k else None
        r1, r3, r5 = ret(1), ret(3), ret(5)
        complete = r5 is not None
        if p["t"] == "sample":
            with db.tx() as c:
                c.execute("UPDATE samples SET ret_1d=?, ret_3d=?, ret_5d=?, evaluated=? WHERE id=?",
                          (r1, r3, r5, 1 if complete else 0, p["id"]))
        else:
            w = rows[:5]
            mu = round((max(r["high"] for r in w) / base - 1) * 100, 2)
            md = round((min(r["low"] for r in w) / base - 1) * 100, 2)
            outcome = "持有中"
            for r in w:
                if p["stop"] and r["low"] <= p["stop"]:
                    outcome = "触发止损"
                    break
                if p["target1"] and r["high"] >= p["target1"]:
                    outcome = "达到止盈1"
                    break
            else:
                if complete:
                    outcome = "5日上涨" if (r5 or 0) > 0 else "5日下跌"
            with db.tx() as c:
                c.execute("UPDATE picks SET ret_1d=?, ret_3d=?, ret_5d=?, max_up_5d=?, max_dn_5d=?, outcome=?, "
                          "evaluated=? WHERE id=?", (r1, r3, r5, mu, md, outcome, 1 if complete else 0, p["id"]))
        done += 1
    return {"updated": done}


# ----------------------------------------------------------------------------- 权重迭代
def iterate(force: bool = False) -> dict:
    from .factors import DEFAULT_WEIGHTS, KEYS, current_weights
    from .strategies import STRATEGIES
    rows = db.q("SELECT date, session, factors, strategies, ret_3d FROM samples WHERE ret_3d IS NOT NULL "
                "ORDER BY id DESC LIMIT 6000")
    if len(rows) < MIN_IC_SAMPLES and not force:
        return {"changed": False, "reason": f"有效样本 {len(rows)} 个，少于 {MIN_IC_SAMPLES}，权重保持不变"}
    groups = {}
    for r in rows:
        groups.setdefault((r["date"], r["session"]), []).append(r)
    ic = {k: [] for k in KEYS}
    for g in groups.values():
        if len(g) < 8:
            continue
        y = [x["ret_3d"] for x in g]
        fs = [db.loads(x["factors"], {}) for x in g]
        for k in KEYS:
            v = spearman([f.get(k, 0) for f in fs], y)
            if v is not None:
                ic[k].append(v)
    ic_mean = {k: (sum(v) / len(v) if v else 0.0) for k, v in ic.items()}
    ic_ir = {}
    for k, v in ic.items():
        if len(v) > 2:
            m = sum(v) / len(v)
            sd = math.sqrt(sum((x - m) ** 2 for x in v) / (len(v) - 1)) or 1e-9
            ic_ir[k] = m / sd
        else:
            ic_ir[k] = 0.0
    ver, old = current_weights()
    target = {k: DEFAULT_WEIGHTS[k] * max(0.3, min(2.0, 1 + 4 * ic_mean[k])) for k in KEYS}
    tsum = sum(target.values())
    target = {k: v / tsum for k, v in target.items()}
    new = {k: 0.7 * old[k] + 0.3 * target[k] for k in KEYS}  # 平滑，单次变化有限
    new = {k: min(0.30, max(0.02, v)) for k, v in new.items()}
    s = sum(new.values())
    new = {k: round(v / s, 4) for k, v in new.items()}

    strat = {}
    for key, name, *_ in STRATEGIES:
        hit = [r["ret_3d"] for r in rows if key in (db.loads(r["strategies"], []) or [])]
        if len(hit) >= 10:
            win = sum(1 for x in hit if x > 0) / len(hit)
            avg = sum(hit) / len(hit)
            w = max(0.5, min(1.5, 0.5 + win + avg / 5))
        else:
            win, avg, w = None, None, 1.0
        strat[key] = {"name": name, "n": len(hit), "win": None if win is None else round(win, 3),
                      "avg": None if avg is None else round(avg, 3), "weight": round(w, 3)}
    moved = sorted(KEYS, key=lambda k: -abs(new[k] - old[k]))[:3]
    note = "；".join(f"{k} {old[k]:.3f}→{new[k]:.3f}（IC {ic_mean[k]:+.3f}）" for k in moved)
    with db.tx() as c:
        c.execute("INSERT INTO weights(created, weights, ic, strat, note) VALUES(?,?,?,?,?)",
                  (now().strftime("%Y-%m-%d %H:%M"), db.dumps(new),
                   db.dumps({"mean": ic_mean, "ir": ic_ir, "groups": len(groups), "samples": len(rows)}),
                   db.dumps(strat), note))
    db.event("learn", "因子权重已迭代", note)
    return {"changed": True, "weights": new, "ic": ic_mean, "strat": strat, "note": note}


# ----------------------------------------------------------------------------- 逻辑回归模型
def _features(raw: dict, sigs) -> list[float]:
    from .factors import KEYS
    from .strategies import STRATEGIES
    ids = set(s["id"] if isinstance(s, dict) else s for s in (sigs or []))
    return [float(raw.get(k, 0) or 0) for k in KEYS] + [1.0 if key in ids else 0.0 for key, *_ in STRATEGIES]


def train() -> dict:
    rows = db.q("SELECT factors, strategies, ret_3d, date FROM samples WHERE ret_3d IS NOT NULL "
                "ORDER BY date DESC, id DESC LIMIT 8000")[::-1]
    if len(rows) < MIN_MODEL_SAMPLES:
        return {"trained": False, "reason": f"样本 {len(rows)} 个，至少需要 {MIN_MODEL_SAMPLES} 个才训练模型"}
    X = [_features(db.loads(r["factors"], {}), db.loads(r["strategies"], [])) for r in rows]
    y = [1.0 if r["ret_3d"] > LABEL_RET else 0.0 for r in rows]
    d = len(X[0])
    mu = [sum(x[j] for x in X) / len(X) for j in range(d)]
    sd = [math.sqrt(sum((x[j] - mu[j]) ** 2 for x in X) / len(X)) or 1.0 for j in range(d)]
    Z = [[(x[j] - mu[j]) / sd[j] for j in range(d)] for x in X]
    cut = int(len(Z) * 0.8)  # 按时间切分：前80%训练，后20%检验（避免偷看未来）
    tr, te = list(range(cut)), list(range(cut, len(Z)))
    w, b, lr, lam = [0.0] * d, 0.0, 0.1, 0.01
    rnd = random.Random(1)
    for _ in range(250):
        rnd.shuffle(tr)
        gw, gb = [0.0] * d, 0.0
        for i in tr:
            p = _sig(sum(w[j] * Z[i][j] for j in range(d)) + b)
            e = p - y[i]
            for j in range(d):
                gw[j] += e * Z[i][j]
            gb += e
        n = len(tr)
        w = [w[j] - lr * (gw[j] / n + lam * w[j]) for j in range(d)]
        b -= lr * gb / n

    def auc(idx):
        ps = [(_sig(sum(w[j] * Z[i][j] for j in range(d)) + b), y[i]) for i in idx]
        pos = [p for p, t in ps if t == 1]
        neg = [p for p, t in ps if t == 0]
        if not pos or not neg:
            return None
        s = sum(1 for a in pos for c in neg if a > c) + 0.5 * sum(1 for a in pos for c in neg if a == c)
        return s / (len(pos) * len(neg))
    test_auc = auc(te)
    top = sorted(te, key=lambda i: -(sum(w[j] * Z[i][j] for j in range(d)) + b))[:max(5, len(te) // 5)]
    metrics = {"n": len(rows), "train": cut, "test": len(te), "base_rate": round(sum(y) / len(y), 3),
               "test_auc": None if test_auc is None else round(test_auc, 3),
               "top20_hit": round(sum(y[i] for i in top) / len(top), 3) if top else None}
    usable = test_auc is not None and test_auc >= 0.55
    metrics["usable"] = usable
    with db.tx() as c:
        c.execute("INSERT INTO models(created, coef, metrics) VALUES(?,?,?)",
                  (now().strftime("%Y-%m-%d %H:%M"), db.dumps({"w": w, "b": b, "mu": mu, "sd": sd}),
                   db.dumps(metrics)))
    db.event("learn", "概率模型已重新训练",
             f"样本 {len(rows)}，检验AUC {metrics['test_auc']}，{'启用' if usable else '效果不够，暂不启用'}")
    _model_cache["ver"] = None
    return {"trained": True, "metrics": metrics}


def _load_model():
    r = db.q1("SELECT version, coef, metrics FROM models ORDER BY version DESC LIMIT 1")
    if not r:
        return None
    if _model_cache["ver"] != r["version"]:
        m = db.loads(r["metrics"], {})
        _model_cache["ver"] = r["version"]
        _model_cache["m"] = db.loads(r["coef"], None) if m.get("usable") else None
    return _model_cache["m"]


def predict(raw: dict, sigs, total: float) -> tuple[float, str]:
    m = _load_model()
    if m and len(m["w"]) == len(_features(raw, sigs)):
        x = _features(raw, sigs)
        z = sum(m["w"][j] * (x[j] - m["mu"][j]) / m["sd"][j] for j in range(len(x))) + m["b"]
        return round(_sig(z), 3), "model"
    # 冷启动：经验映射，未经校准，只用于排序参考
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
        it = iterate()
        tr = train()
        db.event("learn", "历史冷启动完成", f"{len(codes)} 只股票、{len(out)} 个样本；"
                 + (it.get("note") or it.get("reason", "")))
        BOOT["msg"] = f"完成：{len(out)} 个样本"
        return {"samples": len(out), "iterate": it, "model": tr}
    finally:
        BOOT["running"] = False


def daily() -> dict:
    a = fill_outcomes()
    b = iterate()
    c = train()
    return {"outcomes": a, "iterate": b, "model": c}


def summary() -> dict:
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
    return {"version": ver, "weights": w, "labels": LABELS, "history": hist, "model": m,
            "samples": n_s["n"] or 0, "evaluated": n_s["e"] or 0,
            "picks": {"d1": stat("ret_1d"), "d3": stat("ret_3d"), "d5": stat("ret_5d"),
                      "outcomes": {o: sum(1 for p in pk if p["outcome"] == o) for o in
                                   ("达到止盈1", "触发止损", "5日上涨", "5日下跌", "持有中")}},
            "min_ic": MIN_IC_SAMPLES, "min_model": MIN_MODEL_SAMPLES}
