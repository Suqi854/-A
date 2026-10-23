"""AI 接口（自定义，默认 DeepSeek）：个股点评、问答、新闻/公告抓取与情绪打分、盘后复盘、迭代建议。

原则：
- AI 只能基于我们提供的数据说话，提示词里要求它不得编造价格或消息。
- AI 的“迭代建议”必须先通过历史样本回测验证，并且由你点“采用”才会生效，AI 不能直接改实盘参数。
- Key 存在本机保险箱（vault），不会出现在设置文件、日志或公开数据里。
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import db, market, vault
from .config import load_settings, now, today

SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_notes(id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, code TEXT, date TEXT,
  content TEXT, meta TEXT, created TEXT, model TEXT, tokens INTEGER);
CREATE INDEX IF NOT EXISTS ix_ai_notes ON ai_notes(kind, code, date);
CREATE TABLE IF NOT EXISTS news(code TEXT, kind TEXT, date TEXT, title TEXT, url TEXT, source TEXT, fetched TEXT,
  PRIMARY KEY(code, title));
"""

PRESETS = {
    "DeepSeek": {"base_url": "https://api.deepseek.com", "model": "deepseek-chat", "format": "openai"},
    "OpenAI": {"base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini", "format": "openai"},
    "通义千问": {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-plus", "format": "openai"},
    "Kimi": {"base_url": "https://api.moonshot.cn/v1", "model": "moonshot-v1-8k", "format": "openai"},
    "Claude": {"base_url": "https://api.anthropic.com", "model": "claude-sonnet-4-5", "format": "anthropic"},
}
_usage = {"date": None, "calls": 0, "tokens": 0}
_lock = threading.Lock()

SYSTEM = ("你是一名严谨的A股量化研究助手，服务对象是正在学习量化交易的个人投资者。"
          "只能依据用户提供的数据和新闻标题作答，数据里没有的价格、消息、财务数字一律不得编造；"
          "不确定就直说不确定。用简体中文、口语化、条理清楚。"
          "你的输出只是学习研究用的模型观点，不构成投资建议，也不要承诺收益。")


def init() -> None:
    with db.tx() as c:
        c.executescript(SCHEMA)


def cfg() -> dict:
    a = load_settings().get("ai", {})
    p = PRESETS.get(a.get("provider") or "DeepSeek", PRESETS["DeepSeek"])
    return {"provider": a.get("provider") or "DeepSeek", "base_url": a.get("base_url") or p["base_url"],
            "model": a.get("model") or p["model"], "format": a.get("format") or p["format"],
            "daily_limit": int(a.get("daily_limit") or 300), "auto_review": a.get("auto_review", True),
            "auto_sentiment": a.get("auto_sentiment", True), "temperature": float(a.get("temperature", 0.3))}


def ready() -> bool:
    return bool(vault.get("ai_key"))


def usage() -> dict:
    return dict(_usage)


def chat(messages: list[dict], system: str = SYSTEM, json_mode: bool = False, max_tokens: int = 1500,
         timeout: int = 90) -> tuple[str, int]:
    key = vault.get("ai_key")
    if not key:
        raise ValueError("还没有设置 AI 接口 Key（设置 → 密钥保险箱）")
    c = cfg()
    with _lock:
        if _usage["date"] != today():
            _usage.update(date=today(), calls=0, tokens=0)
        if _usage["calls"] >= c["daily_limit"]:
            raise ValueError(f"今天的 AI 调用已达上限 {c['daily_limit']} 次（设置里可调）")
        _usage["calls"] += 1
    if c["format"] == "anthropic":
        url = c["base_url"].rstrip("/") + "/v1/messages"
        body = {"model": c["model"], "max_tokens": max_tokens, "system": system, "messages": messages,
                "temperature": c["temperature"]}
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"}
    else:
        url = c["base_url"].rstrip("/") + "/chat/completions"
        body = {"model": c["model"], "messages": [{"role": "system", "content": system}] + messages,
                "temperature": c["temperature"], "max_tokens": max_tokens}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read())
    except urllib.error.HTTPError as e:
        msg = e.read().decode("utf-8", "ignore")[:300]
        raise RuntimeError(f"AI 接口返回 {e.code}：{msg}") from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise RuntimeError(f"连不上 AI 接口：{e}") from None
    if c["format"] == "anthropic":
        text = "".join(b.get("text", "") for b in d.get("content", []))
        tok = (d.get("usage") or {}).get("input_tokens", 0) + (d.get("usage") or {}).get("output_tokens", 0)
    else:
        text = d["choices"][0]["message"]["content"]
        tok = (d.get("usage") or {}).get("total_tokens", 0)
    _usage["tokens"] += tok
    return text, tok


def chat_json(prompt: str, system: str = SYSTEM, max_tokens: int = 1200) -> dict:
    text, _ = chat([{"role": "user", "content": prompt + "\n\n只输出一个 JSON 对象，不要输出其他文字。"}],
                   system, json_mode=cfg()["format"] == "openai", max_tokens=max_tokens)
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise RuntimeError("AI 没有返回 JSON：" + text[:200])
    return json.loads(m.group(0))


def _save(kind, code, content, meta=None, tokens=0):
    with db.tx() as c:
        c.execute("INSERT INTO ai_notes(kind, code, date, content, meta, created, model, tokens) VALUES(?,?,?,?,?,?,?,?)",
                  (kind, code, today(), content, db.dumps(meta or {}), now().strftime("%Y-%m-%d %H:%M:%S"),
                   cfg()["model"], tokens))


def latest(kind, code="", date=None):
    r = db.q1("SELECT * FROM ai_notes WHERE kind=? AND code=? " + ("AND date=? " if date else "") +
              "ORDER BY id DESC LIMIT 1", (kind, code, date) if date else (kind, code))
    if r:
        r["meta"] = db.loads(r["meta"], {})
    return r


# ----------------------------------------------------------------------------- 新闻与公告抓取
def fetch_news(code: str, name: str | None = None, limit: int = 12) -> list[dict]:
    code = market.norm_code(code)
    out = []
    try:
        url = ("https://np-anotice-stock.eastmoney.com/api/security/ann?sr=-1&page_size=8&page_index=1&ann_type=A"
               "&client_source=web&stock_list=" + code[2:])
        d = json.loads(market.http_get(url, "https://data.eastmoney.com/", retries=1))
        for a in ((d.get("data") or {}).get("list") or [])[:8]:
            out.append({"kind": "公告", "date": (a.get("notice_date") or "")[:10], "title": a.get("title", ""),
                        "url": f"https://data.eastmoney.com/notices/detail/{code[2:]}/{a.get('art_code')}.html",
                        "source": "东方财富公告"})
    except (RuntimeError, ValueError, KeyError):
        pass
    try:
        kw = name or code[2:]
        param = {"uid": "", "keyword": kw, "type": ["cmsArticleWebOld"], "client": "web", "clientType": "web",
                 "clientVersion": "curr", "param": {"cmsArticleWebOld": {"searchScope": "default", "sort": "time",
                                                                          "pageIndex": 1, "pageSize": 10,
                                                                          "preTag": "", "postTag": ""}}}
        url = "https://search-api-web.eastmoney.com/search/jsonp?cb=cb&param=" + urllib.parse.quote(
            json.dumps(param, ensure_ascii=False, separators=(",", ":")))
        raw = market.http_get(url, "https://so.eastmoney.com/", retries=1).decode("utf-8", "ignore")
        d = json.loads(raw[raw.index("(") + 1: raw.rindex(")")])
        for a in ((d.get("result") or {}).get("cmsArticleWebOld") or [])[:10]:
            t = re.sub(r"<[^>]+>", "", a.get("title", ""))
            out.append({"kind": "新闻", "date": (a.get("date") or "")[:16], "title": t, "url": a.get("url", ""),
                        "source": a.get("mediaName", "")})
    except (RuntimeError, ValueError, KeyError):
        pass
    if out:
        with db.tx() as c:
            c.executemany("INSERT OR IGNORE INTO news VALUES(?,?,?,?,?,?,?)",
                          [(code, n["kind"], n["date"], n["title"], n["url"], n["source"],
                            now().strftime("%Y-%m-%d %H:%M")) for n in out])
    out.sort(key=lambda n: n["date"], reverse=True)
    return out[:limit]


# ----------------------------------------------------------------------------- 个股
def _brief(a: dict) -> dict:
    q, pl, mf = a["quote"], a["plan"], a["main_force"]
    return {"股票": f"{a['name']}({a['code']})", "现价": q.get("price"), "涨跌幅%": q.get("pct"), "量比": q.get("vr"),
            "换手%": q.get("turnover"), "市盈率": q.get("pe"), "总市值亿": q.get("mcap"),
            "综合评分": a["score"], "评级": a["rating"], "系统建议": a["advice"],
            "因子得分": {f["label"]: round(f["score"]) for f in a["factors"]},
            "策略信号": [f"{s['name']}：{s['why']}" for s in a["signals"]],
            "交易计划": {"买入区间": pl["buy_zone"], "止损": pl["stop"], "止盈1": pl["target1"], "止盈2": pl["target2"],
                     "盈亏比": pl["rr"], "支撑": pl["supports"], "压力": pl["resistances"]},
            "主力行为": f"{mf['label']}：{mf['desc']}；" + "；".join(mf.get("evidence", [])),
            "风险": a["risks"], "大盘": a["regime"]["state"] + "，" + a["regime"]["desc"]}


def _profile() -> dict:
    try:
        from . import auth
        u = auth.user()
        return (u or {}).get("profile", {})
    except Exception:  # noqa: BLE001
        return {}


def comment(code: str, refresh: bool = False) -> dict:
    from .analysis import analyze
    code = market.norm_code(code)
    if not refresh:
        r = latest("comment", code, today())
        if r:
            return r
    a = analyze(code)
    news = fetch_news(code, a.get("name"))
    prof = _profile()
    prompt = (f"下面是 A量化 系统对一只股票的量化分析数据（JSON）和最近的公告/新闻标题。\n"
              f"【量化数据】{json.dumps(_brief(a), ensure_ascii=False)}\n"
              f"【公告与新闻】{json.dumps([n['date'] + ' ' + n['kind'] + ' ' + n['title'] for n in news], ensure_ascii=False)}\n"
              f"【用户情况】风险偏好：{prof.get('risk', '均衡')}；交易风格：{prof.get('style', '短线')}；"
              f"给自己的规矩：{prof.get('goal', '无')}\n\n"
              "请按下面的结构写点评（Markdown，总共 300~500 字）：\n"
              "### 一句话结论\n### 为什么（量化数据怎么说）\n### 消息面（只基于上面的标题，没有就说没有重要消息）\n"
              "### 操作计划（沿用系统给出的价位；如果你认为要调整，说明理由）\n### 主要风险\n### 学习点（教用户一个知识点）")
    text, tok = chat([{"role": "user", "content": prompt}], max_tokens=1200)
    _save("comment", code, text, {"score": a["score"], "rating": a["rating"], "news": news[:8]}, tok)
    return latest("comment", code)


def ask(code: str, question: str) -> dict:
    from .analysis import analyze
    a = analyze(code)
    news = db.q("SELECT date, kind, title FROM news WHERE code=? ORDER BY date DESC LIMIT 10", (a["code"],))
    prompt = (f"【量化数据】{json.dumps(_brief(a), ensure_ascii=False)}\n"
              f"【近期公告新闻】{json.dumps([n['date'] + ' ' + n['kind'] + ' ' + n['title'] for n in news], ensure_ascii=False)}\n"
              f"【用户问题】{question}\n请结合数据回答，简洁、具体，不超过 300 字。")
    text, tok = chat([{"role": "user", "content": prompt}], max_tokens=800)
    _save("ask", a["code"], f"**问：**{question}\n\n{text}", {}, tok)
    return {"answer": text}


def sentiment(code: str, name: str | None = None) -> dict | None:
    """新闻情绪 -1~1，同一股票一天只问一次。"""
    code = market.norm_code(code)
    r = latest("sentiment", code, today())
    if r:
        return r["meta"]
    news = fetch_news(code, name, 15)
    if not news:
        return None
    titles = [f"{n['date']} {n['kind']} {n['title']}" for n in news]
    d = chat_json("评估下面这些公告/新闻标题对该股票未来几天股价的影响。\n"
                  f"股票：{name or code}\n标题：{json.dumps(titles, ensure_ascii=False)}\n"
                  '输出 {"score": -1到1之间的小数(负面到正面，没有实质消息给0), "reason": "一句话理由", '
                  '"key_news": "最重要的一条标题或空字符串"}', max_tokens=300)
    meta = {"score": max(-1.0, min(1.0, float(d.get("score", 0)))), "reason": str(d.get("reason", ""))[:120],
            "key_news": str(d.get("key_news", ""))[:120]}
    _save("sentiment", code, meta["reason"], meta)
    return meta


# ----------------------------------------------------------------------------- 盘后复盘
def daily_review(force: bool = False) -> dict:
    if not force:
        r = latest("review", "", today())
        if r:
            return r
    from . import learn, picker, sim
    from .analysis import regime
    snap = market.snapshot()
    br = market.breadth(snap)
    runs = [r for r in picker.runs(5) if r["date"] == today()]
    picks = [{"股票": f"{p['name']}({p['code']})", "评分": p["score"], "选出价": p["price"],
              "策略": [s["name"] for s in p["strategies"]]} for r in runs for p in r["items"]]
    ai_acct = sim.view("ai")
    me = sim.view("me")
    t_ai = [t for t in ai_acct["trades"] if t["time"].startswith(today())]
    t_me = [t for t in me["trades"] if t["time"].startswith(today())]
    L = learn.summary()
    data = {"日期": today(), "市场": {"上涨": br["up"], "下跌": br["down"], "涨停": br["limit_up"], "跌停": br["limit_down"],
                                    "成交额亿": br["amount_yi"], "中位涨幅": br["median_pct"]},
            "大盘状态": regime(),
            "今日选股": picks or "无（宁缺毋滥）",
            "AI自动盘": {"总资产": ai_acct["equity"], "累计收益%": ai_acct["total_pct"], "今日成交": [
                f"{t['side']} {t['name']} {t['qty']}股@{t['price']} 盈亏{t['pnl']} {t['reason']}" for t in t_ai]},
            "我的模拟盘": {"总资产": me["equity"], "今日成交": [
                f"{t['side']} {t['name']} {t['qty']}股@{t['price']} 盈亏{t['pnl']} 心态{t['emotion']} {t['reason']}"
                for t in t_me], "按心态统计": me["stats"]["by_emotion"]},
            "选股成绩单": L["picks"], "最近一次因子迭代": (L["history"][0]["note"] if L["history"] else "暂无")}
    prompt = ("请根据下面的当日数据写一份盘后复盘（Markdown，400~700 字）：\n"
              f"{json.dumps(data, ensure_ascii=False, default=str)}\n\n"
              "结构：\n### 今日市场一句话\n### 系统表现（选股是否合理、AI盘操作是否守纪律）\n"
              "### 我的交易点评（结合心态，指出做得好的和要改的；没有交易就说没有）\n"
              "### 明天关注什么（基于数据，不预测涨跌点位）\n### 今日学习点")
    text, tok = chat([{"role": "user", "content": prompt}], max_tokens=1600)
    _save("review", "", text, {"breadth": br}, tok)
    db.event("learn", "AI 盘后复盘已生成", text[:120])
    return latest("review", "")


# ----------------------------------------------------------------------------- 迭代建议（回测把关）
def _eval_weights(weights: dict, rows: list[dict], topk: int = 10) -> dict:
    """在历史样本上：每个(日期,场次)按给定权重打分，取前 topk，看之后3日收益。"""
    from .factors import SPECS, _sig
    groups = {}
    for r in rows:
        groups.setdefault((r["date"], r["session"]), []).append(r)
    rets = []
    for g in groups.values():
        if len(g) < topk * 2:
            continue
        scored = []
        for r in g:
            f = db.loads(r["factors"], {})
            s = sum(_sig(f.get(k, 0), c, sc) * weights.get(k, 0) for k, _, c, sc, _ in SPECS)
            scored.append((s, r["ret_3d"]))
        scored.sort(key=lambda x: -x[0])
        rets += [x[1] for x in scored[:topk]]
    if not rets:
        return {"n": 0}
    return {"n": len(rets), "avg": round(sum(rets) / len(rets), 3),
            "win": round(sum(1 for x in rets if x > 0) / len(rets), 3)}


def suggest_iteration() -> dict:
    from .factors import KEYS, LABELS, current_weights
    from . import learn
    L = learn.summary()
    ver, w = current_weights()
    rows = db.q("SELECT date, session, factors, ret_3d FROM samples WHERE ret_3d IS NOT NULL ORDER BY date")
    if len(rows) < 200:
        raise ValueError(f"有效样本只有 {len(rows)} 个，至少 200 个才能做有意义的建议与验证")
    dates = sorted({r["date"] for r in rows})
    cut = dates[int(len(dates) * 0.7)]
    train = [r for r in rows if r["date"] < cut]
    test = [r for r in rows if r["date"] >= cut]  # 后 30% 时间段做样本外验证
    last = L["history"][0] if L["history"] else {}
    data = {"当前权重": {LABELS[k]: w[k] for k in KEYS}, "因子IC": (last.get("ic") or {}).get("mean"),
            "策略表现": last.get("strat"), "选股成绩单": L["picks"],
            "训练集表现(当前权重)": _eval_weights(w, train)}
    d = chat_json("你是量化研究员。下面是一个多因子选股系统的现状（权重之和为1，单个权重 0.02~0.30）。"
                  "请提出一组新的因子权重，目标是提高“每次评分前10名股票之后3日的平均收益和胜率”，同时避免过拟合。\n"
                  f"{json.dumps(data, ensure_ascii=False, default=str)}\n"
                  '输出 {"weights": {' + ", ".join('"' + k + '": 小数' for k in KEYS) + '}, '
                  '"reason": "100字以内的理由"}（weights 的键必须用上面的英文名）', max_tokens=800)
    nw = {k: max(0.02, min(0.30, float((d.get("weights") or {}).get(k, w[k])))) for k in KEYS}
    s = sum(nw.values())
    nw = {k: round(v / s, 4) for k, v in nw.items()}
    base, cand = _eval_weights(w, test), _eval_weights(nw, test)
    passed = bool(cand.get("n")) and cand["avg"] > base.get("avg", -99) + 0.05 and cand["win"] >= base.get("win", 0) - 0.01
    meta = {"weights": nw, "old": w, "reason": d.get("reason", ""), "test_base": base, "test_new": cand,
            "passed": passed, "cut": cut, "version": ver}
    _save("suggest", "", d.get("reason", ""), meta)
    return latest("suggest", "")


def adopt_suggestion() -> dict:
    r = latest("suggest", "")
    if not r or not r["meta"].get("passed"):
        raise ValueError("没有通过验证的建议可采用")
    m = r["meta"]
    with db.tx() as c:
        c.execute("INSERT INTO weights(created, weights, ic, strat, note) SELECT ?, ?, ic, strat, ? FROM weights "
                  "ORDER BY version DESC LIMIT 1",
                  (now().strftime("%Y-%m-%d %H:%M"), db.dumps(m["weights"]),
                   f"采用AI建议（样本外前10均收益 {m['test_base'].get('avg')}%→{m['test_new'].get('avg')}%）：{m['reason'][:80]}"))
        if not c.execute("SELECT 1 FROM weights").fetchone():
            c.execute("INSERT INTO weights(created, weights, ic, strat, note) VALUES(?,?,?,?,?)",
                      (now().strftime("%Y-%m-%d %H:%M"), db.dumps(m["weights"]), "{}", "{}", "采用AI建议"))
    db.event("learn", "已采用 AI 迭代建议", m["reason"][:120])
    return {"ok": True}


def test_connection() -> dict:
    t0 = time.time()
    text, tok = chat([{"role": "user", "content": "用一句话回复：A量化 AI 接口连接成功。"}], max_tokens=50, timeout=30)
    return {"ok": True, "reply": text.strip(), "ms": int((time.time() - t0) * 1000), "model": cfg()["model"]}
