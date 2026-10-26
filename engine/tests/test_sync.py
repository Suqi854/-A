"""离线测试（5.1.1）：外观设置各端同步、计划模拟进加密个人包（电脑离线时手机网页版可看）。"""
import gzip, json, os, sys, tempfile
os.environ["AQ5_MOCK"] = "1"
os.environ["AQ5_DATA"] = os.environ.get("AQ5_TEST_DATA") or tempfile.mkdtemp()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from aq5 import ai, announce, auth, db, market, marketstate, plan, publisher, sectors, server, signals, sim, vault  # noqa: E402
from aq5.config import load_settings  # noqa: E402

db.init(); [m.init() for m in (auth, ai, sectors, signals, marketstate, announce)]; sim.ensure_accounts(); market.ensure_universe_mock()
sim.can_trade_now = lambda: True

# 1 外观：新的覆盖旧的，旧的不能覆盖新的
r = server.p_look({"theme": "ocean", "ui": "card", "t": 100})
assert r == {"theme": "ocean", "ui": "card", "t": 100}, r
r = server.p_look({"theme": "neon", "t": 50})
assert r["theme"] == "ocean", "更早的修改不应覆盖"
r = server.p_look({"theme": "ths", "t": 200, "evil": "x"})
assert r == {"theme": "ths", "t": 200}, r  # 整体替换（恢复默认时去掉的项不残留），未知字段丢弃
assert load_settings()["look"]["theme"] == "ths"
print("外观同步:", r)

# 2 建一个计划，发布后加密包里要有计划模拟，公开 settings.json 里有外观
snap = market.snapshot()
codes = [x["code"] for x in snap if x.get("price") and 5 < x["price"] < 50][:2]
plan.start({"cash": 300000, "buys": [{"code": codes[0], "amount": 50000}], "holdings": [{"code": codes[1], "qty": 1000, "cost": 0}]})
plan._after_start()
vault.set_many({"mobile_password": "test12345"})
out = publisher.export(full=True)
d = publisher.repo_dir() / "data"
pub = json.loads((d / "settings.json").read_text("utf-8"))
assert pub["look"]["theme"] == "ths", pub.get("look")
priv = json.loads(gzip.decompress(vault.unseal_web((d / "private.enc").read_bytes(), "test12345")))
pl = priv.get("plan")
assert pl and pl["exists"] and pl["cfg"]["cash"] == 300000 and len(pl["account"]["positions"]) >= 1, pl and pl.get("cfg")
assert priv["look"]["theme"] == "ths"
assert set(json.loads((d / "latest.json").read_text("utf-8"))["accounts"]) == {"ai"}, "计划模拟不能出现在公开数据里"
print("加密包里的计划模拟:", pl["mode_name"], "持仓", [p["name"] for p in pl["account"]["positions"]], "| 发布目录", publisher.repo_dir())
print("ALL OK")
