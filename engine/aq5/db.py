"""SQLite 存储：K线缓存、选股记录、学习样本、模拟账户。"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager

from .config import DB_PATH, now

SCHEMA = """
CREATE TABLE IF NOT EXISTS klines(code TEXT, date TEXT, open REAL, close REAL, high REAL, low REAL, vol REAL,
  PRIMARY KEY(code, date));
CREATE TABLE IF NOT EXISTS kline_meta(code TEXT PRIMARY KEY, updated TEXT, n INTEGER);
CREATE TABLE IF NOT EXISTS universe(code TEXT PRIMARY KEY, name TEXT, mcap REAL, fmcap REAL, updated TEXT);
CREATE TABLE IF NOT EXISTS fundflow(code TEXT, date TEXT, main REAL, small REAL, mid REAL, large REAL, xlarge REAL,
  main_pct REAL, PRIMARY KEY(code, date));
CREATE TABLE IF NOT EXISTS pick_runs(id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, session TEXT, started TEXT,
  finished TEXT, scanned INTEGER, candidates INTEGER, picks INTEGER, market TEXT, status TEXT, weights_ver INTEGER);
CREATE TABLE IF NOT EXISTS picks(id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, date TEXT, session TEXT,
  code TEXT, name TEXT, score REAL, prob REAL, price REAL, stop REAL, target1 REAL, target2 REAL, factors TEXT,
  strategies TEXT, reasons TEXT, risks TEXT, ret_1d REAL, ret_3d REAL, ret_5d REAL, max_up_5d REAL,
  max_dn_5d REAL, outcome TEXT, evaluated INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS samples(id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, date TEXT, session TEXT,
  code TEXT, price REAL, score REAL, factors TEXT, strategies TEXT, ret_1d REAL, ret_3d REAL, ret_5d REAL,
  evaluated INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_samples_eval ON samples(evaluated, date);
CREATE TABLE IF NOT EXISTS weights(version INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT, weights TEXT,
  ic TEXT, strat TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS models(version INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT, coef TEXT,
  metrics TEXT);
CREATE TABLE IF NOT EXISTS accounts(id TEXT PRIMARY KEY, name TEXT, cash REAL, init_cash REAL, created TEXT,
  peak REAL);
CREATE TABLE IF NOT EXISTS positions(account TEXT, code TEXT, name TEXT, qty INTEGER, cost REAL, stop REAL,
  target1 REAL, target2 REAL, opened TEXT, last_buy TEXT, source TEXT, pick_id INTEGER, high_since REAL,
  auto INTEGER DEFAULT 1, stage INTEGER DEFAULT 0,
  PRIMARY KEY(account, code));
CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY AUTOINCREMENT, account TEXT, code TEXT, name TEXT,
  side TEXT, kind TEXT, price REAL, qty INTEGER, status TEXT, created TEXT, updated TEXT, fill_price REAL,
  fee REAL, reason TEXT, emotion TEXT, pick_id INTEGER, note TEXT, stop REAL, target1 REAL,
  target2 REAL, auto INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS trades(id INTEGER PRIMARY KEY AUTOINCREMENT, account TEXT, code TEXT, name TEXT,
  side TEXT, qty INTEGER, price REAL, fee REAL, time TEXT, pnl REAL, order_id INTEGER, reason TEXT,
  emotion TEXT, pick_id INTEGER, hold_days INTEGER);
CREATE TABLE IF NOT EXISTS equity(account TEXT, date TEXT, equity REAL, PRIMARY KEY(account, date));
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT, level TEXT, kind TEXT,
  title TEXT, body TEXT, read INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_events_kind ON events(kind, id);
CREATE TABLE IF NOT EXISTS plan(id INTEGER PRIMARY KEY CHECK(id=1), created TEXT, cfg TEXT, mode TEXT, reason TEXT,
  updated TEXT, stance TEXT, history TEXT, active INTEGER DEFAULT 1);
"""

_local = threading.local()
_write_lock = threading.RLock()


def conn() -> sqlite3.Connection:
    c = getattr(_local, "c", None)
    if c is None:
        c = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        _local.c = c
    return c


MIGRATIONS = [("picks", "tier", "TEXT"), ("picks", "rank", "INTEGER"), ("picks", "industry", "TEXT")]


def init() -> None:
    with tx() as c:
        c.executescript(SCHEMA)
        for table, col, typ in MIGRATIONS:
            cols = {r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
            if col not in cols:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")


@contextmanager
def tx():
    with _write_lock:
        c = conn()
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise


def q(sql: str, args=()) -> list[dict]:
    return [dict(r) for r in conn().execute(sql, args).fetchall()]


def q1(sql: str, args=()):
    r = conn().execute(sql, args).fetchone()
    return dict(r) if r else None


def event(kind: str, title: str, body: str = "", level: str = "info") -> None:
    with tx() as c:
        c.execute("INSERT INTO events(time, level, kind, title, body) VALUES(?,?,?,?,?)",
                  (now().strftime("%Y-%m-%d %H:%M:%S"), level, kind, title, body))


def dumps(o) -> str:
    return json.dumps(o, ensure_ascii=False, separators=(",", ":"))


def loads(s, default=None):
    if not s:
        return default
    try:
        return json.loads(s)
    except ValueError:
        return default
