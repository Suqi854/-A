"""微信推送（可选）：PushPlus / Server酱。在“设置”页填入自己的 token 并开启后才会发送。"""
from __future__ import annotations

import json
import threading
import urllib.parse
import urllib.request

from . import db, vault
from .config import MOCK, load_settings


def _post(url: str, data: dict, as_json: bool) -> None:
    body = json.dumps(data).encode() if as_json else urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json" if as_json else "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=8) as r:
        r.read()


def send(title: str, body: str, force: bool = False) -> dict:
    n = dict(load_settings()["notify"])
    n["pushplus_token"] = vault.get("pushplus_token") or n.get("pushplus_token", "")
    n["serverchan_key"] = vault.get("serverchan_key") or n.get("serverchan_key", "")
    if MOCK or not (n.get("enabled") or force):
        return {"sent": False, "reason": "推送未开启"}
    tasks = []
    if n.get("pushplus_token"):
        tasks.append(("PushPlus", "https://www.pushplus.plus/send",
                      {"token": n["pushplus_token"], "title": title, "content": body.replace("\n", "<br>"),
                       "template": "html"}, True))
    if n.get("serverchan_key"):
        tasks.append(("Server酱", f"https://sctapi.ftqq.com/{n['serverchan_key']}.send",
                      {"title": title[:32], "desp": body.replace("\n", "\n\n")}, False))
    if not tasks:
        return {"sent": False, "reason": "没有配置推送 token"}

    def go():
        for name, url, data, js in tasks:
            try:
                _post(url, data, js)
            except Exception as e:  # noqa: BLE001
                db.event("notify", f"{name} 推送失败", str(e)[:200], "warn")
    threading.Thread(target=go, daemon=True).start()
    return {"sent": True, "channels": [t[0] for t in tasks]}
