"""Read-only interpretation of confirmed Chan geometry for the stock report."""

from . import calendar, chan, data

MOVEMENT = {"rising": "中枢上移", "falling": "中枢下移", "overlap": "中枢重叠", "single": "单个确认中枢"}
POSITION = {"above": "收于上沿上方", "inside": "收在中枢内", "below": "收于下沿下方"}


def _center(centers, close):
    if not centers:
        return None
    latest = centers[-1]
    previous = next((x for x in reversed(centers[:-1]) if x["end"] < latest["start"]), None)
    low, high = latest["low"], latest["high"]
    if previous is None:
        movement = "single"
    elif low > previous["high"]:
        movement = "rising"
    elif high < previous["low"]:
        movement = "falling"
    else:
        movement = "overlap"
    position = "above" if close > high else "below" if close < low else "inside"
    return {**latest, "position": position, "movement": movement,
            "previous": {k: previous[k] for k in ("low", "high", "end")} if previous else None}


def review(result, quality):
    chart = result.get("chart", {})
    bars = chart.get("bars", [])
    if not quality["eligible"] or not bars:
        return {"state": "unavailable"}

    geometry = result["structures"]
    close = bars[-1]["close"]
    centers = {}
    for level in ("stroke", "segment"):
        confirmed = [r for r in geometry["centers"] if r["level"] == level and r["confirmed"]]
        centers[level] = _center(confirmed, close)
    # Use one layer end to end. The other layer remains visible as context.
    layer = "stroke" if centers["stroke"] else "segment"
    reference = centers[layer]
    position = reference["position"] if reference else None
    movement = reference["movement"] if reference else None
    state = {("rising", "above"): "up", ("falling", "below"): "down"}.get(
        (movement, position), "neutral" if reference else "waiting")
    observation = {
        "above": "观察回踩能否守住上沿。收回中枢即结束上方观察。",
        "below": "观察反抽能否收回下沿。收回中枢即结束下方观察。",
        "inside": "观察有效离开及随后的回试，再确认中枢外结构。",
    }.get(position, "等待同层中枢确认。")
    points = [r for r in geometry["buy_sell_points"] if r["confirmed"]]
    key = lambda row: (row["level"], row["at"], row["side"])
    ratios = {key(row): row["ratio"] for row in chart["divergence"]}
    divergence = next((row for row in reversed(points) if key(row) in ratios), None)
    if divergence:
        divergence = {**divergence, "ratio": ratios[key(divergence)], "threshold": chan.CONFIG["divergence_rate"]}
    return {"state": state, "close": close, "centers": centers, "reference_layer": layer if reference else None,
            "description": f"{MOVEMENT[movement]}，{POSITION[position]}" if reference else "同层中枢待确认",
            "observation": observation, "latest_point": points[-1] if points else None, "divergence": divergence}


def next_bar_scenario(result, review, asof, sessions):
    """Freeze a next-close direction from agreement between confirmed structure layers."""
    if review["state"] == "unavailable":
        return {"status": "quality_blocked"}
    geometry = result["structures"]
    strokes = [r for r in geometry["strokes"] if r["confirmed"]]
    center = review["centers"].get(review["reference_layer"])
    if center:
        reference = center
        kind, low, high = "center", center["low"], center["high"]
    else:
        reference = strokes[-1] if strokes else None
        if not reference:
            return {"status": "no_reference"}
        kind = "stroke"
        low, high = sorted((reference["start_price"], reference["end_price"]))
    if not low < high:
        return {"status": "no_reference"}
    due = data.next_bar_end(result["timeframe"], result["source_time"], sessions)
    if not due:
        return {"status": "calendar_unavailable"}
    if due <= calendar.stamp(asof):
        return {"status": "expired"}
    close = review["close"]
    bar = result["chart"]["bars"][-1]
    stroke_dir = strokes[-1]["direction"] if strokes else None
    segment = next((r for r in reversed(geometry["segments"]) if r["level"] == "segment" and r["confirmed"]), None)
    segment_dir = segment["direction"] if segment else None
    movement = center["movement"] if center else None
    center_dir = {"rising": "up", "falling": "down"}.get(movement)
    larger = {d for d in (center_dir, segment_dir) if d}
    primary = stroke_dir if stroke_dir and larger == {stroke_dir} else "range"
    checkpoint = None
    if primary != "range":
        endpoints = [low, high] + [r["end_price"] for r in strokes if r["direction"] == primary]
        checkpoint = (min((p for p in endpoints if p > close), default=None) if primary == "up"
                      else max((p for p in endpoints if p < close), default=None))
    return {"version": 1, "status": "ready", "timeframe": result["timeframe"],
            "issued_at": calendar.iso(asof), "source_time": result["source_time"], "due_at": calendar.iso(due),
            "base_close": close, "base_low": bar["low"], "base_high": bar["high"],
            "low": low, "high": high, "primary": primary, "checkpoint": checkpoint,
            "stroke_direction": stroke_dir, "segment_direction": segment_dir, "center_movement": movement,
            "reference_kind": kind, "reference_level": reference["level"], "reference_id": reference["id"],
            "reference_confirmed_at": reference["confirmed_at"]}
