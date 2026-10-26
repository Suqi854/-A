"""离线测试：模拟行情下跑通 选股 -> 模拟成交 -> 止盈止损 -> 回填收益 -> 权重迭代 -> 模型训练。"""
import os, random, sys, tempfile
os.environ["AQ5_MOCK"] = "1"
os.environ["AQ5_DATA"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from aq5 import db, market, picker, sim, learn, analysis, backtest  # noqa: E402

db.init(); sim.ensure_accounts(); market.ensure_universe_mock()
sim.can_trade_now = lambda: True  # 测试时视为交易时间

# 1 选股
r = picker.run("close")
assert r["scanned"] > 100 and r["candidates"] > 50, r
print("选股:", len(r["picks"]), "只", [p["name"] for p in r["picks"]])
# 2 AI 自动盘应已跟随买入
ai = sim.view("ai")
print("AI持仓:", len(ai["positions"]), "现金", ai["cash"])
assert 0 < len(ai["positions"]) <= len(r["picks"])  # 资金用完后不再买（100万 × 仓位上限）
# 3 手动下单 + T+1 校验
code = r["picks"][0]["code"] if r["picks"] else "sh600008"
o = sim.place("me", code, "buy", 300, emotion="逻辑驱动", reason="测试")
assert o["status"] == "filled", o
try:
    sim.place("me", code, "sell", 100); raise SystemExit("T+1 未生效")
except ValueError as e:
    print("T+1 拦截:", e)
try:
    sim.place("me", code, "buy", 150); raise SystemExit("整手校验未生效")
except ValueError as e:
    print("整手拦截:", e)
# 4 模拟次日：把今天买入时间改到前一天，触发止损
with db.tx() as c:
    c.execute("UPDATE trades SET time='2026-01-01 10:00:00'")
    c.execute("UPDATE positions SET opened='2026-01-01 10:00:00', stop=99999")
sim.tick()
me = sim.view("me")
print("止损后持仓:", len(me["positions"]), "交易:", [(t["side"], t["reason"], t["pnl"]) for t in me["trades"]][:3])
assert not me["positions"], "止损没有执行"
# 5 学习：造 400 个历史样本（让 momentum 因子真的有预测力）
rnd = random.Random(3)
with db.tx() as c:
    for d in range(40):
        for k in range(10):
            f = {kk: rnd.gauss(0, 1) for kk in ("momentum", "trend", "volume", "capital", "strength", "volatility",
                                                "value", "position", "liquidity")}
            ret = f["momentum"] * 1.5 + rnd.gauss(0, 2)
            c.execute("INSERT INTO samples(run_id,date,session,code,price,score,factors,strategies,ret_1d,ret_3d,"
                      "ret_5d,evaluated) VALUES(0,?,?,?,?,?,?,?,?,?,?,1)",
                      (f"2026-08-{d % 28 + 1:02d}", "close" if d < 28 else "auction", "sh600008", 10, 70,
                       db.dumps(f), db.dumps(["breakout"] if ret > 0 else []), ret, ret, ret))
it = learn.iterate()
print("迭代:", it["note"])
assert it["weights"]["momentum"] > 0.13, "有效因子权重应上升"
tr = learn.train()
print("模型:", tr)
assert tr["trained"] and tr["metrics"]["test_auc"] > 0.6
# 6 分析 / 回测
a = analysis.analyze(code)
print("分析:", a["name"], a["score"], a["rating"], a["main_force"]["label"], a["plan"]["stop"], a["plan"]["target1"])
b = backtest.run(code)
print("回测:", b["n"], "笔", b["total"], "%", b["verdict"])
print("ALL OK")
