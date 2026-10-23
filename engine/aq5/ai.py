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
            "auto_sentiment": a.get("auto_sentiment", True), "temperature": float(a.get("temperature", 0.3)),
            "auto_explain": a.get("auto_explain", True), "auto_brief": a.get("auto_brief", True)}


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
        if json_mode and "reason" not in c["model"]:  # 推理模型不支持 JSON 模式
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
    db.event("ai", "AI 盘后复盘已生成", "到「迭代」页查看")
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


# ----------------------------------------------------------------------------- 模型列表
def list_models(provider: str | None = None, base_url: str | None = None, fmt: str | None = None) -> dict:
    """向服务商拉取可用模型列表（OpenAI 兼容：GET /models；Claude：GET /v1/models）。"""
    key = vault.get("ai_key")
    if not key:
        raise ValueError("先在「密钥保险箱」填写 AI 接口 Key")
    c = cfg()
    p = PRESETS.get(provider or c["provider"], {})
    base = (base_url or (p.get("base_url") if provider and provider != c["provider"] else c["base_url"])
            or c["base_url"]).rstrip("/")
    fmt = fmt or p.get("format") or c["format"]
    if fmt == "anthropic":
        url, headers = base + "/v1/models?limit=100", {"x-api-key": key, "anthropic-version": "2023-06-01"}
    else:
        url, headers = base + "/models", {"Authorization": "Bearer " + key}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"拉取模型列表失败（{e.code}）：{e.read().decode('utf-8', 'ignore')[:200]}") from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise RuntimeError(f"连不上 AI 接口：{e}") from None
    items = d.get("data") if isinstance(d, dict) else d
    ids = sorted({(m.get("id") if isinstance(m, dict) else str(m)) for m in (items or []) if m})
    chatty = [i for i in ids if not re.search(r"embed|whisper|tts|dall|moderation|audio|image|rerank", i, re.I)]
    return {"models": chatty or ids, "all": len(ids), "base_url": base, "format": fmt}


# ----------------------------------------------------------------------------- 平台上下文（给 AI 的“现在的情况”）
def _ctx(code: str | None = None, light: bool = False) -> dict:
    from . import picker, sim, sectors
    from .analysis import regime
    out: dict = {"时间": now().strftime("%Y-%m-%d %H:%M"), "交易阶段": market.market_phase()}
    try:
        br = market.breadth(market.snapshot())
        out["市场"] = {"上涨": br["up"], "下跌": br["down"], "涨停": br["limit_up"], "跌停": br["limit_down"],
                     "成交额亿": br["amount_yi"], "中位涨幅": br["median_pct"]}
        out["大盘状态"] = regime()
    except Exception:  # noqa: BLE001
        pass
    if not light:
        out["板块"] = sectors.brief(5)
    try:
        me = sim.view("me", light=True)
        out["我的模拟盘"] = {"总资产": me["equity"], "现金": me["cash"], "累计收益%": me["total_pct"],
                         "今日盈亏": me["today_pnl"],
                         "持仓": [f"{p['name']}({p['code'][2:]}) {p['qty']}股 成本{p['cost']:.2f} 现价{p['price']} "
                                f"盈亏{p['pnl_pct']:+.1f}% 止损{p['stop']} 目标{p['target1']} 持有{p['hold_days']}天"
                                for p in me["positions"]]}
    except Exception:  # noqa: BLE001
        pass
    try:
        wl = [market.norm_code(c) for c in load_settings()["watchlist"]][:15]
        qs = market.quotes(wl)
        out["自选股"] = [f"{q.get('name')}({c[2:]}) {q.get('price')} {q.get('pct'):+.2f}%" for c, q in qs.items()
                      if q.get("pct") is not None]
    except Exception:  # noqa: BLE001
        pass
    try:
        rs = picker.runs(2)
        if rs:
            out["最近选股"] = [{"时间": f"{r['date']} {r.get('session')}", "入选": [
                f"{p['name']}({p['code'][2:]}) 评分{p['score']}" for p in r["items"]] or "无（宁缺毋滥）"} for r in rs]
    except Exception:  # noqa: BLE001
        pass
    if code:
        try:
            from .analysis import analyze
            out["当前股票"] = _brief(analyze(code))
        except Exception as e:  # noqa: BLE001
            out["当前股票"] = f"{code} 分析失败：{e}"
    p = _profile()
    out["用户"] = {"风险偏好": p.get("risk", "均衡"), "风格": p.get("style", "短线"), "给自己的规矩": p.get("goal", "无")}
    return out


# ----------------------------------------------------------------------------- 全局 AI 助手
ASSIST_SYS = SYSTEM + ("你嵌在“A量化”交易终端里，能看到平台实时数据（会在消息里给你）。回答要结合这些数据，"
                       "提到股票时写出代码；涉及买卖时必须给出止损位和仓位建议并提醒风险；"
                       "用户问的是平台用法时，简单说明在哪个页面操作（监控/选股/个股/模拟盘/迭代/回测/日志/设置）。")


def assistant(messages: list[dict], page: str = "", code: str | None = None) -> dict:
    msgs = [{"role": m["role"], "content": str(m["content"])[:2000]} for m in messages[-10:]
            if m.get("role") in ("user", "assistant") and m.get("content")]
    if not msgs or msgs[-1]["role"] != "user":
        raise ValueError("请输入问题")
    ctx = _ctx(code)
    msgs[-1] = {"role": "user", "content": f"【平台实时数据】{json.dumps(ctx, ensure_ascii=False, default=str)}\n"
                                           f"【我在看的页面】{page or '未知'}\n【我的问题】{msgs[-1]['content']}"}
    text, tok = chat(msgs, ASSIST_SYS, max_tokens=1200)
    _save("chat", code or "", f"**问：**{messages[-1]['content'][:300]}\n\n{text}", {"page": page}, tok)
    return {"answer": text, "tokens": tok}


# ----------------------------------------------------------------------------- 选股解读
def explain_run(run_id: int, force: bool = False) -> dict:
    from . import picker, sectors
    if not force:
        r = latest("run", str(run_id))
        if r:
            return r
    run = next((x for x in picker.runs(30) if x["id"] == run_id), None)
    if not run:
        raise ValueError("找不到这次选股")
    cands = picker.top_candidates(run_id, 8)
    data = {"场次": f"{run['date']} {run.get('session')}", "扫描": run.get("scanned"), "精评": run.get("candidates"),
            "门槛": (run.get("market") or {}).get("min_score"), "大盘": (run.get("market") or {}).get("regime"),
            "入选": [{"股票": f"{p['name']}({p['code'][2:]})", "行业": p.get("industry"), "评分": p["score"],
                    "现价": p["price"], "止损": p["stop"], "目标1": p["target1"],
                    "理由": p["reasons"][:4], "风险": p["risks"][:3]} for p in run["items"]],
            "落选的高分候选": [f"{c.get('name')}({c['code'][2:]}) {c['score']}分" for c in cands[:6]],
            "板块": sectors.brief(5)}
    prompt = ("下面是 A量化 一次自动选股的结果。请写一份简短解读（Markdown，250~450 字）：\n"
              f"{json.dumps(data, ensure_ascii=False, default=str)}\n\n"
              "结构：\n### 这次选股的结论（有机会还是观望，为什么）\n"
              "### 逐只点评（每只一两句：机会在哪、最大风险、怎么操作；没有入选就分析落选的高分股差在哪）\n"
              "### 和板块的关系（入选股是否在强势行业）\n### 学习点")
    text, tok = chat([{"role": "user", "content": prompt}], max_tokens=1100)
    _save("run", str(run_id), text, {"picks": len(run["items"])}, tok)
    return latest("run", str(run_id))


def explain_run_async(run_id: int) -> None:
    def go():
        try:
            explain_run(run_id)
            if load_settings().get("publish", {}).get("enabled"):
                from . import publisher
                publisher.publish_async(full=True)
        except Exception as e:  # noqa: BLE001
            db.event("system", "AI 选股解读失败", str(e)[:150], "warn")
    threading.Thread(target=go, daemon=True, name="aq5-ai-run").start()


# ----------------------------------------------------------------------------- 下单前风控检查
def order_check(code: str, side: str, qty: int, price: float | None = None) -> dict:
    from .analysis import analyze
    from . import sim
    a = analyze(code)
    me = sim.view("me", light=True)
    pos = next((p for p in me["positions"] if p["code"] == a["code"]), None)
    px = price or a["quote"].get("price") or 0
    amt = px * qty
    data = {"下单": {"方向": "买入" if side == "buy" else "卖出", "股票": f"{a['name']}({a['code']})", "数量": qty,
                   "价格": px, "金额": round(amt, 2), "占总资产%": round(amt / me["equity"] * 100, 1) if me["equity"] else None},
            "账户": {"总资产": me["equity"], "现金": me["cash"], "持仓数": len(me["positions"]),
                   "这只股票现有持仓": (f"{pos['qty']}股 成本{pos['cost']:.2f} 盈亏{pos['pnl_pct']:+.1f}%" if pos else "无")},
            "量化数据": _brief(a), "行业": a.get("sector"), "用户": _ctx(light=True).get("用户")}
    d = chat_json("你是交易风控助手。根据下面的数据，判断这笔模拟单是否符合纪律（仓位、止损、趋势、大盘、用户自己定的规矩）。\n"
                  f"{json.dumps(data, ensure_ascii=False, default=str)}\n"
                  '输出 {"verdict": "通过" 或 "谨慎" 或 "不建议", "points": ["最多4条要点，每条20字以内"], '
                  '"stop": 建议止损价(数字), "position": "建议仓位，如 10%", "tip": "一句话提醒"}', max_tokens=500)
    out = {"verdict": d.get("verdict") if d.get("verdict") in ("通过", "谨慎", "不建议") else "谨慎",
           "points": [str(x)[:60] for x in (d.get("points") or [])][:4], "stop": d.get("stop"),
           "position": str(d.get("position", ""))[:20], "tip": str(d.get("tip", ""))[:100],
           "system": {"score": a["score"], "rating": a["rating"], "stop": a["plan"]["stop"]}}
    _save("check", a["code"], f"{out['verdict']}：{out['tip']}", out)
    return out


# ----------------------------------------------------------------------------- 单笔交易复盘
def trade_review(trade_id: int, force: bool = False) -> dict:
    if not force:
        r = latest("trade", str(trade_id))
        if r:
            return r
    t = db.q1("SELECT * FROM trades WHERE id=?", (trade_id,))
    if not t:
        raise ValueError("找不到这笔成交")
    buys = db.q("SELECT * FROM trades WHERE account=? AND code=? AND side='buy' AND id<? ORDER BY id DESC LIMIT 3",
                (t["account"], t["code"], trade_id))
    try:
        rows = market.kline(t["code"], 40)[-25:]
    except RuntimeError:
        rows = []
    data = {"这笔成交": t, "之前的买入": buys,
            "近25日K线(日期,开,收,高,低)": [(r["date"], r["open"], r["close"], r["high"], r["low"]) for r in rows],
            "用户": _ctx(light=True).get("用户")}
    prompt = ("请复盘这笔模拟交易（Markdown，200~350 字）：\n"
              f"{json.dumps(data, ensure_ascii=False, default=str)}\n\n"
              "结构：\n### 结果\n### 做对了什么\n### 可以改进什么（买点、卖点、仓位、心态；结合K线说具体）\n### 下次怎么做")
    text, tok = chat([{"role": "user", "content": prompt}], max_tokens=900)
    _save("trade", str(trade_id), text, {"code": t["code"], "pnl": t["pnl"]}, tok)
    return latest("trade", str(trade_id))


# ----------------------------------------------------------------------------- 盘前简报 / 持仓体检
def morning_brief(force: bool = False) -> dict:
    if not force:
        r = latest("brief", "", today())
        if r:
            return r
    ctx = _ctx()
    try:
        ov = market.quotes(load_settings().get("overseas", []))
        ctx["外围市场"] = [f"{q.get('name')} {q.get('pct'):+.2f}%" for q in ov.values() if q.get("pct") is not None]
    except Exception:  # noqa: BLE001
        pass
    rv = latest("review", "")
    if rv:
        ctx["上一次盘后复盘摘要"] = rv["content"][:600]
    prompt = ("现在是开盘前（或盘中），请根据数据写一份今日简报（Markdown，300~500 字）：\n"
              f"{json.dumps(ctx, ensure_ascii=False, default=str)}\n\n"
              "结构：\n### 一句话看今天\n### 关注的板块（基于数据，说明理由）\n"
              "### 我的持仓怎么处理（逐只：继续拿/减仓/到止损就走；没有持仓就说空仓等机会）\n"
              "### 今天的纪律提醒（结合用户自己的规矩）")
    text, tok = chat([{"role": "user", "content": prompt}], max_tokens=1200)
    _save("brief", "", text, {}, tok)
    db.event("ai", "AI 今日简报已生成", "到「监控」页查看")
    return latest("brief", "")


def holdings_check() -> dict:
    from . import sim
    from .analysis import analyze
    me = sim.view("me", light=True)
    if not me["positions"]:
        raise ValueError("「我的模拟盘」现在没有持仓")
    items = []
    for p in me["positions"][:8]:
        try:
            a = analyze(p["code"])
            items.append({"持仓": f"{p['name']}({p['code'][2:]}) {p['qty']}股 成本{p['cost']:.2f} 盈亏{p['pnl_pct']:+.1f}% "
                                f"持有{p['hold_days']}天 止损{p['stop']} 目标{p['target1']}",
                          "评分": a["score"], "评级": a["rating"], "主力": a["main_force"]["label"],
                          "行业": (a.get("sector") or {}).get("name"), "行业强度": (a.get("sector") or {}).get("score"),
                          "风险": a["risks"][:3]})
        except Exception:  # noqa: BLE001
            continue
    prompt = ("请给下面的模拟盘持仓做体检（Markdown，每只 2~3 句，最后总结仓位）：\n"
              f"{json.dumps({'持仓': items, '账户': {'总资产': me['equity'], '现金': me['cash']}, '用户': _ctx(light=True).get('用户')}, ensure_ascii=False, default=str)}\n"
              "每只给出：持有/减仓/止损离场 之一，以及理由和具体价位。")
    text, tok = chat([{"role": "user", "content": prompt}], max_tokens=1200)
    _save("holdings", "", text, {"n": len(items)}, tok)
    return latest("holdings", "")


def notes(kinds=("brief", "review", "run", "holdings"), limit: int = 30) -> list[dict]:
    q = ",".join("?" * len(kinds))
    rows = db.q(f"SELECT id, kind, code, date, created, content, meta FROM ai_notes WHERE kind IN ({q}) "
                f"ORDER BY id DESC LIMIT ?", (*kinds, limit))
    for r in rows:
        r["meta"] = db.loads(r["meta"], {})
    return rows


# ----------------------------------------------------------------------------- 选股跟踪归因
def pick_review(pick_id: int, force: bool = False) -> dict:
    if not force:
        r = latest("pickreview", str(pick_id))
        if r:
            return r
    from . import tracking
    it = next((x for x in tracking.track(30)["items"] if x["id"] == pick_id), None)
    if not it:
        raise ValueError("找不到这只入选股的跟踪数据")
    data = {k: it.get(k) for k in ("date", "session", "name", "code", "tier", "rank", "score", "price", "stop", "target1",
                                    "strategy", "industry", "verdict", "path", "max_up", "max_dn", "alpha", "bench", "first", "why")}
    data["选出时因子分"] = {LABELS_CN.get(k, k): v for k, v in (it.get("parts") or {}).items()}
    prompt = ("下面是一只被系统选出的股票之后的真实走势和初步归因。请做一次复盘（Markdown，200~350 字）：\n"
              f"{json.dumps(data, ensure_ascii=False, default=str)}\n\n"
              "结构：\n### 结论（符合预期吗）\n### 为什么（结合大盘、行业、量能、买点位置、因子）\n### 对策略的改进建议（具体到因子/门槛/买点）")
    text, tok = chat([{"role": "user", "content": prompt}], max_tokens=900)
    _save("pickreview", str(pick_id), text, {"verdict": it["verdict"]}, tok)
    return latest("pickreview", str(pick_id))


try:
    from .factors import LABELS as LABELS_CN
except Exception:  # noqa: BLE001
    LABELS_CN = {}
