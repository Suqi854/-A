"""全局配置：路径、时区、可编辑设置。只用标准库，Windows 直接可跑。"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("AQ5_DATA", ROOT / "data"))
WEB = ROOT / "web"
try:
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / ".w").write_text("ok")
except OSError:  # 插件缓存目录可能只读，改用用户目录
    DATA = Path.home() / ".aq5"
    DATA.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA / "aq5.sqlite3"
SETTINGS_PATH = DATA / "settings.json"

CN_TZ = timezone(timedelta(hours=8))  # 不依赖 tzdata，Windows 也稳
MOCK = os.environ.get("AQ5_MOCK") == "1"  # 离线演示/测试用的模拟行情


def now() -> datetime:
    return datetime.now(CN_TZ)


def today() -> str:
    return now().strftime("%Y-%m-%d")


DEFAULTS = {
    "port": 18950,
    "host": "0.0.0.0",           # 0.0.0.0 才能让手机在同一 WiFi 下访问
    "watchlist": ["sh600519", "sz000858", "sh601318", "sh600036", "sz000333"],
    "indices": ["sh000001", "sz399001", "sz399006", "sh000300", "sh000016", "sh000688"],
    "overseas": ["hkHSI", "usIXIC", "usDJI", "usINX"],
    "init_cash": 100000.0,
    "fees": {"commission": 0.00025, "min_commission": 5.0, "stamp_tax": 0.0005, "transfer": 0.00001},
    "slippage": 0.001,
    "pick": {
        "times": ["09:26", "14:45"],
        "max_picks": 5,
        "min_score": 72.0,          # 综合分门槛：宁缺毋滥
        "min_amount_yi": 1.0,       # 成交额至少 1 亿（尾盘），竞价按比例
        "exclude_st": True,
        "min_listed_days": 120,
        "auto_trade": True,         # AI 自动账户是否跟随选股模拟买入
        "position_pct": 0.2,        # AI 账户每只票仓位
    },
    "risk": {"stop_atr": 2.0, "max_stop_pct": 0.08, "rr_1": 1.5, "rr_2": 3.0, "max_hold_days": 10},
    "notify": {"pushplus_token": "", "serverchan_key": "", "enabled": False},
    "refresh_sec": 3,
    # 公开发布：把行情、选股、分析、AI自动盘、学习结果导出为 JSON + 只读网页，推送到 Git 的 data 分支
    # “我的模拟盘”、自选股、推送 token 永远不会被导出
    "publish": {"enabled": False, "remote": "https://github.com/Suqi854/-A.git", "branch": "data",
                "push": True, "interval_min": 10, "repo_dir": ""},
    # 个人数据加密备份到 GitHub 私有仓库（备份密码在本机保险箱）
    "backup": {"enabled": True, "remote": "", "time": "15:50", "keep_days": 7},
    "net": {"direct_cn": True},
    "security": {"local_no_login": True},
    # AI 接口（Key 在本机保险箱）：provider 可选 DeepSeek / OpenAI / 通义千问 / Kimi / Claude / 自定义
    "ai": {"provider": "DeepSeek", "base_url": "", "model": "", "format": "", "daily_limit": 300,
           "auto_review": True, "auto_sentiment": True, "temperature": 0.3,
           "auto_explain": True, "auto_brief": True},
}

_lock = threading.Lock()


def _merge(base: dict, extra: dict) -> dict:
    out = dict(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_settings() -> dict:
    with _lock:
        if SETTINGS_PATH.exists():
            try:
                return _merge(DEFAULTS, json.loads(SETTINGS_PATH.read_text("utf-8")))
            except (OSError, ValueError):
                pass
        return json.loads(json.dumps(DEFAULTS))


def save_settings(patch: dict) -> dict:
    cur = load_settings()
    merged = _merge(cur, patch)
    with _lock:
        tmp = SETTINGS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2), "utf-8")
        os.replace(tmp, SETTINGS_PATH)
    return merged


def public_settings() -> dict:
    s = load_settings()
    n = s.get("notify", {})
    try:
        from . import vault
        st = vault.status()
    except Exception:  # noqa: BLE001
        st = {}
    s["notify"] = {**n, "pushplus_token": "已设置" if (n.get("pushplus_token") or "pushplus_token" in st) else "",
                   "serverchan_key": "已设置" if (n.get("serverchan_key") or "serverchan_key" in st) else ""}
    return s
