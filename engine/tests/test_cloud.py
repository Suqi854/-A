"""离线测试（5.1.2）：云端交易日值守——电脑在线只待命、连续 3 分钟离线才接手、电脑回来交还并接力、到时接力。"""
import os, sys, tempfile
from datetime import datetime
os.environ["AQ5_MOCK"] = "1"
os.environ["AQ5_DATA"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from aq5 import cloud, hf, market, scheduler  # noqa: E402
from aq5.config import CN_TZ  # noqa: E402

clock = {"t": 0.0}
cloud.time.sleep = lambda s: clock.__setitem__("t", clock["t"] + s)
cloud.time.time = lambda: clock["t"]
cloud.now = lambda: datetime(2026, 10, 28, 10, 0, tzinfo=CN_TZ).replace(minute=int(clock["t"] // 60) % 60)
market.is_trading_day = lambda: True
cloud.remote_meta = lambda: {"owner": "pc", "updated": "2026-10-28 09:58:00"}
cloud._gh_raw = lambda p: b"x"
cloud.unpack = lambda blob, take_settings: {"tables": 7}
log = []
cloud.upload = lambda active, final=False: log.append(("upload", active, final)) or {"ok": True}
cloud.dispatch_next = lambda reason: log.append(("dispatch", reason)) or True
hf.start = lambda: log.append(("hf",))
scheduler.start = lambda: log.append(("sched",))


def run(alive_seq, max_minutes=340):
    seq = list(alive_seq)
    cloud.pc_alive = lambda: seq.pop(0) if seq else seq_last[0]
    seq_last = [alive_seq[-1]]
    log.clear(); clock["t"] = 0.0
    cloud.run_cloud(max_minutes=max_minutes)
    return list(log)


# 1 电脑一直在线：只待命，到时接力，不交易
r = run([True] * 10, max_minutes=5)
assert ("sched",) not in r and r[-1][0] == "dispatch", r
print("一直在线:", r)

# 2 抖动 2 分钟（不到 3 次）不接手
r = run([True, False, False, True] + [True] * 5, max_minutes=8)
assert ("sched",) not in r, r
print("抖动不接手:", r)

# 3 连续 3 分钟离线 → 接手；之后电脑回来 → 交还并接力
r = run([True, False, False, False, False, False, True], max_minutes=60)
assert ("sched",) in r and ("hf",) in r and ("upload", True, False) in r, r
assert r[-2] == ("dispatch", "交还后继续值守") and r[-1] == ("upload", False, True), r
print("接手并交还:", r)
print("ALL OK")
