"""Read-only timing probe of the existing cloud stock-analysis handler."""
import json
import threading
import time
from collections import defaultdict
from functools import wraps
from urllib.parse import urlsplit

from aq5 import analysis, decision, decision_collect, decision_emotion, decision_growth, market, marketstate, sectors
from aq5.forecast import chan, data, service

start = time.perf_counter()
events = defaultdict(list)
lock = threading.Lock()


def watch(module, name, subkey=None):
    original = getattr(module, name)
    label = module.__name__.removeprefix("aq5.") + "." + name

    @wraps(original)
    def timed(*args, **kwargs):
        beginning = time.perf_counter()
        failed = False
        try:
            return original(*args, **kwargs)
        except Exception:
            failed = True
            raise
        finally:
            end = time.perf_counter()
            key = label + ("@" + subkey(args) if subkey else "")
            with lock:
                events[key].append((beginning - start, end - start, failed))

    setattr(module, name, timed)


for mod, names in (
    (market, ("snapshot", "_em_snapshot", "_em_page", "_tencent_snapshot", "kline_with_live", "fundflow", "cross_check")),
    (marketstate, ("state",)),
    (sectors, ("of_stock", "overview",)),
    (analysis, ("analyze",)),
    (decision_collect, ("collect",)),
    (decision_growth, ("collect",)),
    (decision_emotion, ("collect", "_boards", "_members", "_history", "_focus_fallback", "_sina_fallback")),
    (decision, ("load_forecast", "view")),
    (data, ("adjustment_input", "_qualified_snapshot", "context", "fetch")),
    (chan, ("analyze",)),
    (service, ("analyze",)),
):
    for name in names:
        if hasattr(mod, name):
            watch(mod, name)
watch(market, "http_get", lambda args: urlsplit(args[0]).hostname or "unknown")

try:
    result = decision_collect.analysis("sh600522")
    print("AQ5_PROFILE_RESULT " + json.dumps({
        "wall_s": round(time.perf_counter() - start, 2),
        "frames": {tf: r["quality"]["eligible"] for tf, r in result["forecast"]["structures"].items()},
        "routes": sorted(result["fusion"]["routes"]),
    }), flush=True)
finally:
    for name, rows in sorted(events.items()):
        print("AQ5_PROFILE " + json.dumps({
            "stage": name, "calls": len(rows), "errors": sum(row[2] for row in rows),
            "start_s": round(min(row[0] for row in rows), 2),
            "end_s": round(max(row[1] for row in rows), 2),
            "longest_s": round(max(b - a for a, b, _ in rows), 2),
            "sum_s": round(sum(b - a for a, b, _ in rows), 2),
        }), flush=True)
