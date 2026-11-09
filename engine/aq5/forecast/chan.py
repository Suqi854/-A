"""Point-in-time adapter for the pinned, unmodified chan.py structure engine.

This module performs no network, database, or file writes. Input `date` is the
completed bar END, and available_at records when that revision was known.
"""
from __future__ import annotations

import copy
import hashlib
import importlib
import json
import math
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

VERSION = "chan-structure-v1"
UPSTREAM_COMMIT = "f8ca3b5e44d7166e2abd756a8e7fa816c79faebe"
CN = timezone(timedelta(hours=8))
VENDOR = Path(__file__).resolve().parents[2] / "vendor" / "chan"
CONFIG = {
    "bi_algo": "normal", "bi_strict": True, "bi_fx_check": "strict",
    "gap_as_kl": False, "bi_end_is_peak": True, "bi_allow_sub_peak": False,
    "seg_algo": "chan", "left_seg_method": "peak", "zs_algo": "normal",
    "zs_combine": True, "zs_combine_mode": "zs", "one_bi_zs": False,
    "macd_algo": "area", "macd_algo-seg": "area", "divergence_rate": 0.9,
    "min_zs_cnt": 1, "bs1_peak": True, "bs_type": "1,1p,2,2s,3a,3b",
    "bsp2_follow_1": True, "bsp3_follow_1": True, "bsp2s_follow_2": True,
    "strict_bsp3": True, "trigger_step": True,
    "print_warning": False, "print_err_time": False,
}


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


CONFIG_HASH = _hash({"version": VERSION, "commit": UPSTREAM_COMMIT,
                     "configuration": CONFIG, "segment_area_adapter": 1})
_loader_lock = threading.Lock()
_engine = None


def verify_vendor():
    """Verify the vendored distribution against its checked-in provenance."""
    manifest = json.loads((VENDOR / "PROVENANCE.json").read_text("utf-8"))
    if manifest.get("commit") != UPSTREAM_COMMIT or manifest.get("license") != "MIT":
        raise RuntimeError("chan_vendor_provenance_mismatch")
    for name, expected in manifest["files"].items():
        path = (VENDOR / name).resolve()
        if not path.is_relative_to(VENDOR.resolve()) or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise RuntimeError("chan_vendor_hash_mismatch")
    return {"commit": manifest["commit"], "files": len(manifest["files"]), "license": "MIT"}


def _load_engine():
    global _engine
    with _loader_lock:
        if _engine is not None:
            return _engine
        verify_vendor()
        # Upstream uses top-level imports. Reject conflicting installed packages
        # rather than silently binding its classes to a different Common/Math.
        for name in ("Chan", "ChanConfig", "Common", "Math", "Bi", "Seg", "ZS", "KLine",
                     "Combiner", "BuySellPoint", "ChanModel", "DataAPI"):
            mod = sys.modules.get(name)
            if mod is not None:
                origin = getattr(mod, "__file__", None)
                if origin is not None and not Path(origin).resolve().is_relative_to(VENDOR.resolve()):
                    raise RuntimeError("chan_import_namespace_collision")
        if str(VENDOR) not in sys.path:
            sys.path.insert(0, str(VENDOR))
        CChan = importlib.import_module("Chan").CChan
        CChanConfig = importlib.import_module("ChanConfig").CChanConfig
        CTime = importlib.import_module("Common.CTime").CTime
        enums = importlib.import_module("Common.CEnum")
        CKLine_Unit = importlib.import_module("KLine.KLine_Unit").CKLine_Unit
        CSeg = importlib.import_module("Seg.Seg").CSeg
        original_metric = CSeg.cal_macd_metric

        def segment_metric(self, algo, is_reverse):
            # The upstream segment only supports slope/amp. Extend AREA with
            # exactly the signed-histogram convention used by its CBi.AREA.
            # Raw vendor bytes remain unchanged; this extension is versioned.
            if algo != enums.MACD_ALGO.AREA:
                return original_metric(self, algo, is_reverse)
            unit, end = self.get_begin_klu(), self.get_end_klu()
            total = 1e-7
            while unit is not None and unit.idx <= end.idx:
                value = unit.macd.macd
                if (self.is_down() and value < 0) or (self.is_up() and value > 0):
                    total += abs(value)
                unit = unit.next
            return total

        CSeg.cal_macd_metric = segment_metric
        _engine = CChan, CChanConfig, CTime, enums, CKLine_Unit
        return _engine


def _stamp(value, *, daily=False):
    if isinstance(value, datetime):
        t = value
    elif isinstance(value, str):
        if daily and len(value) == 10:
            value += "T15:00:00"
        t = datetime.fromisoformat(value)
    else:
        raise ValueError("timestamp_missing")
    return t.astimezone(CN) if t.tzinfo else t.replace(tzinfo=CN)


def _iso(value):
    return value.isoformat(timespec="seconds")


def _number(value):
    if isinstance(value, bool):
        raise ValueError("invalid_price")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("invalid_price")
    return value


def _normalize(bars, tf, asof):
    result, reasons, seen = [], set(), set()
    if not isinstance(bars, (list, tuple)):
        return [], {"invalid_bars"}
    # A later revision is selected ONLY if available at asof. The root caller
    # persists previous snapshots, never replacing previous prediction inputs.
    revisions = {}
    for raw in bars:
        try:
            end = _stamp(raw.get("end_at") or raw.get("date"), daily=tf == "day")
            if end > asof or raw.get("complete") is False:
                continue
            available = _stamp(raw["available_at"]) if raw.get("available_at") else end
            if available > asof:
                continue
            if available < end:
                reasons.add("available_before_bar_end")
                continue
            row = {k: _number(raw[k]) for k in ("open", "high", "low", "close")}
            row["vol"] = _number(raw.get("vol", raw.get("volume", 0)))
            if min(row[k] for k in ("open", "high", "low", "close")) <= 0 or row["vol"] < 0:
                raise ValueError("invalid_price")
            if row["low"] > min(row["open"], row["close"]) or row["high"] < max(row["open"], row["close"]):
                raise ValueError("invalid_ohlc")
            row.update(date=_iso(end), available_at=_iso(available), source=str(raw.get("source") or ""))
            if not raw.get("available_at"):
                reasons.add("availability_unknown")
            if not row["source"]:
                reasons.add("source_unknown")
            adjustment = raw.get("adjustment")
            if adjustment not in ("none", "pit") or raw.get("adjustment_verified") is not True:
                reasons.add("adjustment_unverified")
            if adjustment == "pit":
                if not raw.get("adjustment_asof") or _stamp(raw["adjustment_asof"]) > asof:
                    reasons.add("adjustment_not_point_in_time")
            if raw.get("continuity_verified") is False:
                reasons.add("bar_gap")
            identity = (row["date"], row["available_at"])
            digest = _hash(row)
            if identity in seen and revisions.get(identity, (None,))[0] != digest:
                reasons.add("conflicting_revision")
            seen.add(identity)
            revisions[identity] = (digest, row)
        except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
            reasons.add("invalid_bar")
    latest = {}
    for (_, _), (_, row) in sorted(revisions.items()):
        latest[row["date"]] = row
    result = [latest[key] for key in sorted(latest)]
    # Different bar histories may legitimately have different acquisition times.
    # Enforce chronology for first confirmations without pretending historical
    # data fetched today was observed in real time.
    maximum = None
    for row in result:
        maximum = max(maximum or row["available_at"], row["available_at"])
        row["known_at"] = maximum
    if len(result) < 60:
        reasons.add("insufficient_bars")
    if len(result) > 4000:
        return [], reasons | {"bar_budget_exceeded"}
    return result, reasons


def _geometry(book, rows, enums):
    """Extract fresh JSON primitives; no upstream mutable objects escape."""
    def date(unit):
        return rows[unit.idx]["date"]

    def line(item, level):
        return {"level": level, "start": date(item.get_begin_klu()), "end": date(item.get_end_klu()),
                "start_price": float(item.get_begin_val()), "end_price": float(item.get_end_val()),
                "direction": "up" if item.is_up() else "down", "confirmed": bool(item.is_sure)}

    out = {"klines": [], "fractals": [], "strokes": [], "segments": [], "centers": [], "buy_sell_points": []}
    for k in book:
        out["klines"].append({"start": date(k.lst[0]), "end": date(k.lst[-1]),
                              "high": float(k.high), "low": float(k.low), "count": len(k.lst),
                              "direction": k.dir.name.lower()})
        if k.fx != enums.FX_TYPE.UNKNOWN:
            out["fractals"].append({"at": date(k.get_peak_klu(k.fx == enums.FX_TYPE.TOP)),
                                     "kind": k.fx.name.lower(), "high": float(k.high), "low": float(k.low),
                                     "confirmed": k.next is not None})
    out["strokes"] = [line(b, "stroke") for b in book.bi_list]
    out["segments"] = [line(s, "segment") for s in book.seg_list]
    out["segments"] += [line(s, "segment2") for s in book.segseg_list]
    for level, centers in (("stroke", book.zs_list), ("segment", book.segzs_list)):
        for center in centers:
            out["centers"].append({"level": level, "start": date(center.begin), "end": date(center.end),
                                    "low": float(center.low), "high": float(center.high),
                                    "peak_low": float(center.peak_low), "peak_high": float(center.peak_high),
                                    "confirmed": bool(center.is_sure), "extensions": len(center.bi_lst),
                                    "merged_count": len(center.sub_zs_lst)})
    for level, points in (("stroke", book.bs_point_lst), ("segment", book.seg_bs_point_lst)):
        for point in points.bsp_iter():
            # A sure stroke alone is insufficient when its containing segment
            # is still virtual. Require the complete parent structure as well.
            parent = point.bi.parent_seg
            confirmed = bool(point.bi.is_sure and parent is not None and parent.is_sure)
            out["buy_sell_points"].append({"level": level, "at": date(point.klu),
                                           "price": float(point.bi.get_end_val()),
                                           "side": "buy" if point.is_buy else "sell",
                                           "types": sorted(t.value for t in point.type),
                                           "confirmed": confirmed,
                                           "macd_area": float(point.bi.cal_macd_metric(enums.MACD_ALGO.AREA, False))})
    for key in out:
        out[key].sort(key=lambda x: (x.get("end", x.get("at", "")), x.get("level", ""), _hash(x)))
    return out


def _annotate(snapshot, history, previous, events, at, scope):
    current = {}
    for kind, objects in snapshot.items():
        if kind == "klines":
            continue
        for obj in objects:
            identity = _hash({"scope": scope, "kind": kind, "geometry": {k: v for k, v in obj.items() if k != "confirmed"}})
            old = history.get(identity)
            if old is None:
                old = history[identity] = {"observed_at": at, "confirmed_at": None}
            if obj.get("confirmed") and old["confirmed_at"] is None:
                old["confirmed_at"] = at
                events.append({"event_id": _hash([identity, "confirmed", at]), "structure_id": identity,
                               "kind": kind, "event": "confirmed", "known_at": at})
            elif obj.get("confirmed") and previous.get(identity) is not True:
                events.append({"event_id": _hash([identity, "reconfirmed", at]), "structure_id": identity,
                               "kind": kind, "event": "reconfirmed", "known_at": at})
            obj.update(id=identity, **old)
            current[identity] = obj.get("confirmed", False)
    for identity, was_confirmed in previous.items():
        if identity not in current and was_confirmed:
            events.append({"event_id": _hash([identity, "superseded", at]), "structure_id": identity,
                           "event": "superseded", "known_at": at})
        elif was_confirmed and not current.get(identity):
            events.append({"event_id": _hash([identity, "withdrawn", at]), "structure_id": identity,
                           "event": "withdrawn", "known_at": at})
    return current


def _features(structures, close, rows):
    confirmed = lambda name: [x for x in structures[name] if x.get("confirmed")]
    strokes, segments, centers, points = (confirmed(k) for k in ("strokes", "segments", "centers", "buy_sell_points"))
    last = lambda xs: xs[-1] if xs else {}
    stroke, segment, center, point = map(last, (strokes, segments, centers, points))
    direction = lambda item: 1.0 if item.get("direction") == "up" else -1.0 if item.get("direction") == "down" else 0.0
    features = {"chan_stroke_count": float(len(strokes)), "chan_segment_count": float(len(segments)),
                "chan_center_count": float(len(centers)), "chan_stroke_direction": direction(stroke),
                "chan_segment_direction": direction(segment), "chan_center_present": float(bool(center)),
                "chan_buy_point": float(point.get("side") == "buy"),
                "chan_sell_point": float(point.get("side") == "sell"),
                "chan_center_position": 0.0, "chan_center_width": 0.0,
                "chan_macd_area": float(point.get("macd_area", 0.0)),
                "chan_point_age_bars": float(sum(r["date"] > point["at"] for r in rows)) if point else 4001.0,
                "chan_point_confirmation_age_bars": float(sum(r["known_at"] > point["confirmed_at"] for r in rows)) if point else 4001.0}
    if center:
        mid = (center["high"] + center["low"]) / 2
        features["chan_center_position"] = close / mid - 1
        features["chan_center_width"] = (center["high"] - center["low"]) / mid
    for type_ in ("1", "1p", "2", "2s", "3a", "3b"):
        features["chan_point_" + type_] = float(type_ in point.get("types", []))
    return {k: v for k, v in features.items() if math.isfinite(v)}


def analyze(code, timeframe, bars, asof):
    """Compute the entire currently-known structure chain, fail closed on gaps.

    Adjustment evidence is explicit: adjustment='none'|'pit',
    adjustment_verified=True; pit additionally requires adjustment_asof.
    A caller may render unknown-quality structures, but must not trade them.
    """
    tf = {"d": "day", "1d": "day", "day": "day", "m60": "60m", "60m": "60m",
          "m15": "15m", "15m": "15m", "m5": "5m", "5m": "5m"}.get(timeframe)
    t = _stamp(asof)
    empty = {k: [] for k in ("klines", "fractals", "strokes", "segments", "centers", "buy_sell_points", "revisions")}
    out = {"version": VERSION, "upstream_commit": UPSTREAM_COMMIT, "config_hash": CONFIG_HASH,
           "code": str(code), "timeframe": tf or str(timeframe), "asof": _iso(t),
           "quality": {"eligible": False, "reason": "uninitialized"}, "confirmed": False,
           "structures": empty, "features": {}, "bar_count": 0}
    if tf is None:
        out["quality"]["reason"] = "unsupported_timeframe"
        out["id"] = _hash(out)
        return out
    rows, reasons = _normalize(bars, tf, t)
    out["bar_count"] = len(rows)
    out["input_hash"] = _hash(rows)
    if not rows:
        reasons.add("missing_bars")
    else:
        try:
            CChan, Cfg, CTime, enums, Unit = _load_engine()
            kind = {"day": enums.KL_TYPE.K_DAY, "60m": enums.KL_TYPE.K_60M,
                    "15m": enums.KL_TYPE.K_15M, "5m": enums.KL_TYPE.K_5M}[tf]
            engine = CChan(str(code), lv_list=[kind], config=Cfg(copy.deepcopy(CONFIG)))
            history, previous, events = {}, {}, []
            scope = [str(code), tf, CONFIG_HASH]
            for row in rows:
                end = _stamp(row["date"])
                unit = Unit({enums.DATA_FIELD.FIELD_TIME: CTime(end.year, end.month, end.day, end.hour, end.minute,
                                                               end.second, auto=False),
                             "open": row["open"], "high": row["high"], "low": row["low"],
                             "close": row["close"], "volume": row["vol"]})
                engine.trigger_load({kind: [unit]})
                snapshot = _geometry(engine[kind], rows, enums)
                previous = _annotate(snapshot, history, previous, events, row["known_at"], scope)
            snapshot["revisions"] = events
            out.update(structures=snapshot, features=_features(snapshot, rows[-1]["close"], rows),
                       source_time=rows[-1]["date"], known_at=rows[-1]["known_at"])
        except Exception as exc:
            # Third-party structure failures must block this observation rather
            # than escape and kill the shared forecasting worker.
            reasons.add("engine_error:" + type(exc).__name__)
    out["quality"] = {"eligible": not reasons, "reason": ";".join(sorted(reasons)) if reasons else "ok",
                      "input_bars": len(rows), "adjustment_required": True}
    out["confirmed"] = not reasons and bool([x for x in out["structures"]["strokes"] if x.get("confirmed")])
    out["id"] = _hash({k: v for k, v in out.items() if k != "id"})
    return out


def align_levels(results):
    """Report agreement/conflict of already point-in-time-safe level results."""
    valid = [r for r in results if r.get("quality", {}).get("eligible") and r.get("confirmed")]
    directions = {r["timeframe"]: int(r.get("features", {}).get("chan_segment_direction", 0)) for r in valid}
    nonzero = {v for v in directions.values() if v}
    return {"eligible": len(valid) == len(results) and bool(results), "directions": directions,
            "state": "conflict" if len(nonzero) > 1 else "aligned" if nonzero else "waiting_structure",
            "direction": next(iter(nonzero)) if len(nonzero) == 1 else 0,
            "structure_ids": [r["id"] for r in valid]}
