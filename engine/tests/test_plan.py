"""离线测试（5.1）：计划模拟（建立 → 选策略 → 跟选股 → 做T → 对照收益）、日志按类型取数、新策略、快速迭代、高频自调。"""
import os, sys, tempfile
os.environ["AQ5_MOCK"] = "1"
os.environ["AQ5_DATA"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from aq5 import db, learn, market, picker, plan, server, sim, signals, strategies  # noqa: E402

db.init(); signals.init(); sim.ensure_accounts(); market.ensure_universe_mock()
sim.can_trade_now = lambda: True

# 1 没建立计划时：不出现在账户列表，view 返回可选策略
assert "plan" not in sim.existing(), sim.existing()
v = plan.view()
assert v["exists"] is False and set(v["modes"]) == {"attack", "rotate", "t_trade", "defense"}

# 2 参数校验
for bad in ({"cash": 0}, {"cash": 1000, "buys": [{"code": "600519", "amount": 5000}]}, {"cash": 1000, "holdings": [{"code": "600519", "qty": 0}]}):
    try:
        plan.start(bad); raise SystemExit(f"应拒绝 {bad}")
    except ValueError as e:
        print("拦截:", e)

# 3 建立：现金 50 万、开局买两只（一只按金额、一只按股数）、已有持仓一只
snap = market.snapshot()
codes = [r["code"] for r in snap if r.get("price") and 5 < r["price"] < 50][:3]
pv = plan.preview({"cash": 500000, "buys": [{"code": codes[0], "amount": 100000}, {"code": codes[1], "qty": 1050}],
                   "holdings": [{"code": codes[2], "qty": 3000}]})
assert pv["ok"] and pv["buys"][1]["qty"] == 1000 and pv["buys"][0]["qty"] % 100 == 0, pv
assert pv["mode_guess"] in plan.MODES and pv["cash_after"] < 500000, pv
print("预览:", pv["mode_name"], "|", pv["reason"], "| 开局买入约", pv["spend"])
assert not plan.preview({"cash": 1000, "buys": [{"code": codes[0], "amount": 50}]})["ok"]
assert not plan.preview({"cash": 500000, "buys": [{"code": codes[0], "amount": 1000}], "holdings": [{"code": codes[0], "qty": 100}]})["ok"]
r = plan.start({"cash": 500000, "risk": "均衡", "mode": "auto",
                "buys": [{"code": codes[0], "amount": 100000}, {"code": codes[1], "amount": 80000}],
                "holdings": [{"code": codes[2], "qty": 3000, "cost": 0}]})
print(r["message"])
plan._after_start()  # 同步跑完（正常是后台线程）
v = plan.view()
a = v["account"]
print("计划:", v["mode_name"], "|", v["reason"])
assert v["exists"] and v["active"] and v["mode"] in plan.MODES
assert len(a["positions"]) == 3, [p["code"] for p in a["positions"]]
held0 = next(p for p in a["positions"] if p["code"] == codes[2])
assert held0["qty"] == 3000 and held0["avail"] == 3000, held0  # 初始持仓当天就能卖
assert a["init"] == v["cfg"]["init_total"]
assert v["baseline"] and abs(v["baseline"]["pct"] - a["total_pct"]) < 1.0, (v["baseline"], a["total_pct"])
assert "plan" in sim.existing()
bought = [p for p in a["positions"] if p["code"] in codes[:2]]
assert all(p["avail"] == 0 for p in bought), "开局买入的当天不能卖（T+1）"

# 4 策略选择：现金占比 ≥60% → 进攻；锁定后跟随锁定
cash_ratio = a["cash"] / a["equity"]
print("现金占比", round(cash_ratio, 3))
res = plan.set_mode("t_trade")
assert res["mode"] == "t_trade" and "锁定" in res["reason"], res
res = plan.set_mode("auto")
assert res["mode"] in plan.MODES

# 5 跟选股：进攻/轮动模式应买入候选
plan.set_mode("attack")
pr = picker.run("close")
v2 = plan.view()
n_new = len(v2["account"]["positions"]) - 3
print("选股后计划持仓:", len(v2["account"]["positions"]), "新增", n_new)
assert n_new > 0, "进攻模式应跟随选股买入"
assert any(t["reason"].startswith("计划·进攻跟随") for t in v2["account"]["trades"])

# 6 防守：只卖不买
plan.set_mode("defense")
before = len(db.q("SELECT id FROM orders WHERE account='plan' AND side='buy'"))
plan.on_picks(pr["picks"], "close")
after = len(db.q("SELECT id FROM orders WHERE account='plan' AND side='buy'"))
assert after == before, "防守模式不应买入"
sells = db.q("SELECT reason FROM orders WHERE account='plan' AND side='sell'")
print("防守卖出:", [s["reason"][:30] for s in sells])
assert any("防守减仓" in s["reason"] for s in sells)

# 7 做T：手动构造触发
plan.set_mode("t_trade")
db.q("SELECT 1")  # noqa
with db.tx() as c:  # 防守可能已卖掉初始持仓：补一笔可卖的持仓给做T用
    c.execute("INSERT OR REPLACE INTO positions(account, code, name, qty, cost, opened, last_buy, source, high_since, auto, stage) "
              "VALUES('plan', ?, 'T测试', 3000, 10, '2026-01-05 10:00:00', '2026-01-05 10:00:00', '初始持仓', 10, 1, 0)", (codes[2],))
code = codes[2]
from aq5.config import now as real_now  # noqa: E402
plan.now = lambda: real_now().replace(hour=10, minute=0)  # 测试按上午盘中跑（14:50 后只做收尾）
real_levels, real_check = signals.levels, signals.check_t
lv = real_levels(code)
signals.levels = lambda c, a=None: lv
signals.check_t = lambda c, l, n=None, record=True, account="watch": [{"strategy": "vwap_high", "side": "sell", "price": l["price"], "time": "10:00"}]
plan.tick()
sold = plan._t_qty(code, "sell")
assert sold >= 100 and sold % 100 == 0, sold
signals.check_t = lambda c, l, n=None, record=True, account="watch": [{"strategy": "vwap_low", "side": "buy", "price": l["price"], "time": "10:30"}]
plan.tick()
assert plan._t_qty(code, "buy") == sold, (plan._t_qty(code, "buy"), sold)
signals.levels, signals.check_t = real_levels, real_check
print("做T：高抛", sold, "股，接回", plan._t_qty(code, "buy"), "股")

# 8 战绩统计 / 降级
st = plan.mode_stats()
print("各策略战绩:", {k: (x["n"], x["win"]) for k, x in st.items()})

# 9 暂停 / 恢复
plan.stop(); assert not plan.active()
assert all(not p["auto"] for p in db.q("SELECT auto FROM positions WHERE account='plan'"))
plan.resume(); assert plan.active()

# 10 重置保护：计划模拟不能用普通重置
try:
    server.p_reset({"acct": "plan"}); raise SystemExit("计划模拟不应被普通重置")
except ValueError as e:
    print("重置拦截:", e)

# 11 日志按类型取数：每类最多 N 条，计划类事件存在
for i in range(30):
    db.event("trade", f"刷屏成交 {i}")
ev = server.api_events({"per_kind": ["5"]})
kinds = {}
for e in ev:
    kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
print("日志分类条数:", kinds)
assert max(kinds.values()) <= 5 and "plan" in kinds and "pick" in kinds

# 12 新策略能被检测器调用、公式齐全
ids = [s[0] for s in strategies.STRATEGIES]
assert {"limit_pullback", "pocket_pivot", "rs_high"} <= set(ids) and all(k in strategies.FORMULAS for k in ids)
hit = {}
for r0 in snap[:300]:
    rows = market.kline(r0["code"], 130, history=True)
    if len(rows) < 70:
        continue
    from aq5.factors import raw_factors
    raw, ind = raw_factors(rows, None)
    for s in strategies.detect(rows, ind, None, "close", {}):
        hit[s["id"]] = hit.get(s["id"], 0) + 1
print("策略触发次数（模拟行情 300 只）:", hit)

# 13 迭代：1 日收益先反馈 + 实战平仓回灌；午间快速迭代；高频自调
it = learn.iterate(force=True)
assert it["changed"] and "_meta" in it["strat"], it
print("迭代:", it["note"])
print("快速迭代:", learn.quick()["model"])
print("高频自调:", learn.tune_hf())
sw = strategies.strategy_weights("close")
assert all(0.5 <= w <= 1.5 for w in sw.values()), sw
print("ALL OK")

# 14 （5.1.3）大盘立场去抖：新立场持续 10 分钟才采用，「防守」立即生效
import time as _tm  # noqa: E402
plan._stance_seen.update(stance=None, since=0.0)
assert plan._steady_stance("谨慎", "中性") == "中性"          # 刚出现：先不变
plan._stance_seen["since"] = _tm.time() - 601
assert plan._steady_stance("谨慎", "中性") == "谨慎"          # 持续 10 分钟：采用
assert plan._steady_stance("防守", "中性") == "防守"          # 风险立场立即生效
# 现金多就进攻（谨慎也进攻，只是仓位减半）
assert plan._decide(0.9, 0, 0, {"stance": "谨慎"}, "均衡", "auto", None)[0] == "attack"
print("5.1.3 去抖与进攻规则 OK")

# 15 （5.1.3）盘中机会：候选回到买入区就买（每轮最多一只）
plan.set_mode("attack")
_r = plan._row(); _r["cfg"]["risk"] = "积极"  # 前面的测试已买满「均衡」的 6 只，换成「积极」（最多 8 只）
with db.tx() as _c:
    _c.execute("UPDATE plan SET cfg=? WHERE id=1", (db.dumps(_r["cfg"]),))
    _c.execute("UPDATE accounts SET cash=cash+300000 WHERE id='plan'")
before = len(db.q("SELECT id FROM orders WHERE account='plan' AND reason LIKE '%盘中机会%'"))
cand = [x for x in snap if x.get("price") and 5 < x["price"] < 40 and x["code"] not in {p["code"] for p in plan.view()["account"]["positions"]}][0]
plan._scan.update(t=0.0, cands=[{"code": cand["code"], "name": cand.get("name"), "score": 80, "tier": "B", "id": None}])
real_plan = signals.plan
signals.plan = lambda code, phase=None, acct="me", save=True: {"name": cand.get("name"), "actions": ["买入"], "score": 80, "rating": "买入",
                                                              "price": cand["price"], "summary": "测试", "levels": {"stop": cand["price"] * 0.93,
                                                              "target1": cand["price"] * 1.1, "target2": cand["price"] * 1.2}}
plan._scan_entries("attack")
signals.plan = real_plan
after = len(db.q("SELECT id FROM orders WHERE account='plan' AND reason LIKE '%盘中机会%'"))
assert after == before + 1, (before, after)
print("盘中机会买入 OK")
