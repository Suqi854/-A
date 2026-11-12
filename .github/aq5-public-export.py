import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path.cwd()))
from aq5 import ai, announce, auth, cloud, db, marketstate, publisher, sectors, signals
from aq5.config import now

def run():
    data = Path.cwd().parent / "pages-data" / "data"
    if now().hour < 17 or not (data / "latest.json").is_file():
        raise RuntimeError("closed_session_required")
    previous = json.loads((data / "latest.json").read_text("utf-8"))
    meta = cloud.remote_meta()
    if not meta or meta.get("active") is not False or not meta.get("_state_commit"):
        raise RuntimeError("ledger_not_closed")
    # The established cloud restore reads the account bundle into runner scratch.
    # The only staged outputs are the same public AI and overview views served today.
    blob = cloud.read_remote_state(ref=meta["_state_commit"])
    if not blob or cloud.remote_meta() != meta:
        raise RuntimeError("ledger_changed")
    db.init()
    for mod in (auth, ai, sectors, signals, marketstate, announce):
        mod.init()
    cloud.unpack(blob, take_settings=True)
    snapshot = publisher._ai_view()
    overview = publisher.public_overview()
    newest = snapshot["trades"][0]["time"]
    if not newest.startswith("2026-11-12") or set(overview["accounts"]) != {"ai"}:
        raise RuntimeError("unexpected_public_export")
    if cloud.remote_meta() != meta:
        raise RuntimeError("ledger_changed")
    overview["private_pack_sha256"] = previous.get("private_pack_sha256")
    publisher._w(data / "ai.json", snapshot)
    publisher._w(data / "latest.json", overview)
    print(json.dumps({"observation": overview["time"], "latest_sim_fill": newest}))

try:
    run()
except Exception as exc:
    print(type(exc).__name__, file=sys.stderr)
    raise SystemExit(1) from None
