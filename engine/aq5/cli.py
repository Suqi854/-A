"""命令行入口（给 Claude / Codex 插件技能调用，也可以自己在终端用）。

    python -m aq5.cli market                 大盘总览
    python -m aq5.cli quote 600519 000858    实时报价
    python -m aq5.cli analyze 600519         个股分析 + 交易计划 + 主力行为
    python -m aq5.cli pick                   立即全主板选股
    python -m aq5.cli backtest 600519 [策略]  个股回测
    python -m aq5.cli public latest|picks|ai|learn|analysis 600519 [--base URL]
                                              读取已发布到 GitHub 的公开数据（不直连行情源）

加 --json 输出原始 JSON。只读行情与本机模拟，不连接券商、不下真实订单。
"""
from __future__ import annotations

import json
import sys
import urllib.request

DEFAULT_BASE = "https://raw.githubusercontent.com/Suqi854/-A/data/data"


def _p(v, d=2):
    return "--" if v is None else f"{v:.{d}f}"


def _pct(v):
    return "--" if v is None else f"{v:+.2f}%"


def out(obj, as_json: bool, text: str):
    from .server import clean
    print(json.dumps(clean(obj), ensure_ascii=False, indent=1) if as_json else text)


def md_analysis(a: dict) -> str:
    q, pl, mf = a["quote"], a["plan"], a["main_force"]
    L = [f"## {a['name']}（{a['code']}）现价 {_p(q.get('price'))}  {_pct(q.get('pct'))}",
         f"**评级：{a['rating']}**　综合 {a['score']}（因子 {a['fscore']} / 策略 {a['sscore']}）　"
         f"{'模型' if a.get('prob_src') == 'model' else '经验'}估计3日上涨概率 {a['prob'] * 100:.0f}%　大盘：{a['regime']['state']}",
         f"> {a['advice']}", "",
         "### 交易计划",
         f"- 买入区间 {_p(pl['buy_zone'][0])} ~ {_p(pl['buy_zone'][1])}",
         f"- 止损 {_p(pl['stop'])}（{_pct(pl['stop_pct'])}）　止盈① {_p(pl['target1'])}（{_pct(pl['target1_pct'])}）　"
         f"止盈② {_p(pl['target2'])}（{_pct(pl['target2_pct'])}）　盈亏比 {pl['rr']}",
         f"- 支撑 {' / '.join(_p(x) for x in pl['supports']) or '--'}；压力 {' / '.join(_p(x) for x in pl['resistances']) or '--'}"]
    L += [f"- {r}" for r in pl["rules"]]
    L += ["", f"### 主力行为：{mf['label']}", mf["desc"]] + [f"- {x}" for x in mf.get("evidence", [])]
    L += ["", "### 因子得分", "| 因子 | 得分 | 权重 |", "|---|---|---|"]
    L += [f"| {f['label']} | {f['score']:.0f} | {f['weight'] * 100:.1f}% |" for f in a["factors"]]
    L += ["", "### 策略信号"] + ([f"- **{s['name']}**（置信 {s['conf'] * 100:.0f}%）：{s['why']}" for s in a["signals"]]
                                or ["- 当前未触发买点策略"])
    L += ["", "### 看多理由"] + ([f"- {x}" for x in a["reasons"]] or ["- 暂无明显优势"])
    L += ["", "### 风险"] + ([f"- {x}" for x in a["risks"]] or ["- 暂无明显风险项"])
    L += ["", "### 学习要点"] + [f"- {x}" for x in a["lessons"]]
    L += ["", "_模型输出，仅供学习与模拟，不构成投资建议。_"]
    return "\n".join(L)


def md_picks(r: dict) -> str:
    if not r:
        return "还没有选股记录。"
    items = next((x for x in (r.get("items"), r.get("picks")) if isinstance(x, list)), [])
    head = (f"## 选股结果 {r.get('date', '')} {r.get('session', '')}　扫描 {r.get('scanned')} 只 · "
            f"精评 {r.get('candidates')} 只")
    if not items:
        return head + "\n\n**宁缺毋滥：本次没有达到门槛的机会，建议观望。**"
    L = [head, ""]
    for p in items:
        pl = p.get("plan") or p
        sig = p.get("signals") or p.get("strategies") or []
        L.append(f"### {p['name']}（{p['code']}）评分 {p['score']}　现价 {_p(p['price'])}")
        L.append(f"止损 {_p(pl.get('stop'))}　止盈① {_p(pl.get('target1'))}　止盈② {_p(pl.get('target2'))}　"
                 f"策略：{'、'.join(s['name'] for s in sig[:3])}")
        L += [f"- 机会：{x}" for x in p.get("reasons", [])[:4]]
        L += [f"- 风险：{x}" for x in p.get("risks", [])]
        L.append("")
    return "\n".join(L)


def md_market(ov: dict) -> str:
    L = ["## 大盘总览 " + ov.get("time", ""), "| 指数 | 点位 | 涨跌 |", "|---|---|---|"]
    L += [f"| {q['name']} | {_p(q['price'])} | {_pct(q.get('pct'))} |" for q in ov.get("indices", []) + ov.get("overseas", [])]
    b, rg = ov.get("breadth") or {}, ov.get("regime") or {}
    if b.get("total"):
        L.append(f"\n主板 上涨 {b['up']} / 下跌 {b['down']}，涨停 {b['limit_up']} / 跌停 {b['limit_down']}，"
                 f"成交 {b['amount_yi']} 亿，主力净额 {b['main_net_yi']} 亿，中位涨幅 {_pct(b['median_pct'])}")
    L.append(f"大盘状态：**{rg.get('state', '--')}** —— {rg.get('desc', '')}")
    if ov.get("top"):
        L.append("\n涨幅前列：" + "，".join(f"{x['name']} {_pct(x['pct'])}" for x in ov["top"][:8]))
    return "\n".join(L)


def fetch_public(path: str, base: str):
    with urllib.request.urlopen(f"{base.rstrip('/')}/{path}", timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in argv
    argv = [a for a in argv if a != "--json"]
    base = DEFAULT_BASE
    if "--base" in argv:
        i = argv.index("--base")
        base = argv[i + 1]
        del argv[i:i + 2]
    if not argv:
        print(__doc__)
        return 0
    cmd, args = argv[0], argv[1:]
    if cmd == "public":
        what = args[0] if args else "latest"
        if what == "analysis":
            from .market import norm_code
            a = fetch_public(f"analysis/{norm_code(args[1])}.json", base)
            a.pop("kline", None)
            return out(a, as_json, md_analysis(a))
        path = {"latest": "latest.json", "picks": "picks/latest.json", "runs": "runs.json", "ai": "ai.json",
                "learn": "learn.json", "index": "analysis/index.json", "manifest": "manifest.json"}[what]
        d = fetch_public(path, base)
        text = md_market(d) if what == "latest" else md_picks(d) if what == "picks" else json.dumps(d, ensure_ascii=False)[:4000]
        return out(d, as_json, text)
    from . import db, market, sim
    db.init()
    sim.ensure_accounts()
    if cmd == "quote":
        qs = market.quotes(args)
        text = "\n".join(f"{q['name']}（{c}）{_p(q.get('price'))} {_pct(q.get('pct'))} 量比 {_p(q.get('vr'))} "
                         f"换手 {_p(q.get('turnover'))}% 时间 {q.get('time')}" for c, q in qs.items())
        return out(qs, as_json, text or "没有取到行情")
    if cmd == "analyze":
        from .analysis import analyze
        a = analyze(args[0])
        return out(a, as_json, md_analysis(a))
    if cmd == "market":
        from .server import api_overview
        ov = api_overview({})
        ov.pop("accounts", None)
        ov.pop("watchlist", None)
        return out(ov, as_json, md_market(ov))
    if cmd == "pick":
        from . import picker
        r = picker.run(args[0] if args else "manual")
        return out(r, as_json, md_picks({**r, "date": "", "session": ""}))
    if cmd == "backtest":
        from .backtest import run
        r = run(args[0], args[1] if len(args) > 1 else "score")
        r.pop("curve", None)
        text = (f"{r['strategy_name']} 回测 {r['start']}~{r['end']}：收益 {_pct(r['total'])}（持股不动 {_pct(r['bench'])}），"
                f"{r['n']} 笔，胜率 {r['win_rate']}，最大回撤 {r['max_dd']}%。{r['verdict']}")
        return out(r, as_json, text)
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main() or 0)
