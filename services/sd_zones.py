"""Actionable Demand & Supply zones near the current price.

One engine for the chart, the analysis panel, the technical scanner, the
signal card and the chart backtest. Every zone comes from real OHLCV bars:

* structure: a base of 1-4 small candles (or one rejection candle) followed
  by an impulsive departure of at least MIN_DEPARTURE_ATR x ATR, named by the
  leg-in/leg-out (RBR, DBR, RBD, DBD or Rejection);
* lifecycle from the bars after the departure only: number of tests, depth
  of penetration, and a close beyond the far edge, which breaks the zone;
* strength from departure, volume, break of structure, base quality,
  freshness and reactions;
* selection: valid zones on the correct side of price, within a distance
  limit derived from daily ATR, nearest first; overlapping zones from
  different timeframes merge into one confluence zone.

Nothing is invented: when no valid zone sits inside the limit the side is
reported as "No nearby valid ... Zone".
"""

import math

import numpy as np
import pandas as pd

TIMEFRAMES = ("1d", "1wk", "1mo")
LABELS = {"1d": "Daily", "1wk": "Weekly", "1mo": "Monthly"}
RESAMPLE = {"1wk": "W", "1mo": "ME"}
# Daily bars read per timeframe: about 3 years for daily structure, 10 years
# for weekly, 20 years for monthly.
LOOKBACK_DAILY_BARS = {"1d": 750, "1wk": 2500, "1mo": 5000}
# (ATR multiple, floor %, cap %) for the maximum distance from price.
DISTANCE_RULES = {
    "1d": (5.0, 3.0, 12.0),
    "1wk": (8.0, 5.0, 20.0),
    "1mo": (10.0, 6.0, 30.0),
}
TIMEFRAME_WEIGHT = {"1d": 1.0, "1wk": 1.15, "1mo": 1.3}
ATR_PERIOD = 14
MAX_BASE = 4
DEPARTURE_BARS = 3
MIN_DEPARTURE_ATR = 1.5
MIN_SCORE = 45
STRONG_SCORE = 70
MODERATE_SCORE = 50
PER_SIDE = 2
DISPLAY_BARS = 40
NO_DEMAND = "No nearby valid Demand Zone"
NO_SUPPLY = "No nearby valid Supply Zone"


def clean_frame(df):
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    frame = df.copy()
    for column in ("Open", "High", "Low", "Close"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame["Volume"] = pd.to_numeric(frame["Volume"], errors="coerce").fillna(0) if "Volume" in frame else 0.0
    frame = frame.dropna(subset=["Open", "High", "Low", "Close"])
    index = pd.DatetimeIndex(frame.index)
    if index.tz is not None:
        index = index.tz_convert("Asia/Kolkata").tz_localize(None)
    frame.index = index
    frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    return frame[["Open", "High", "Low", "Close", "Volume"]]


def timeframe_frame(daily, timeframe):
    source = daily.tail(LOOKBACK_DAILY_BARS.get(timeframe, len(daily)))
    rule = RESAMPLE.get(timeframe)
    if not rule or source.empty:
        return source
    return source.resample(rule).agg({
        "Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum",
    }).dropna(subset=["Open", "High", "Low", "Close"])


def atr_series(frame, period=ATR_PERIOD):
    high, low, close = frame["High"], frame["Low"], frame["Close"]
    true_range = pd.concat([
        high - low, (high - close.shift()).abs(), (low - close.shift()).abs(),
    ], axis=1).max(axis=1)
    return true_range.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def _period_start(stamp, timeframe):
    stamp = pd.Timestamp(stamp)
    if timeframe == "1wk":
        return (stamp - pd.Timedelta(days=6)).normalize()
    if timeframe == "1mo":
        return stamp.replace(day=1).normalize()
    return stamp


def _epoch(stamp):
    return int(pd.Timestamp(stamp).timestamp())


def strength_label(score):
    if score >= STRONG_SCORE:
        return "Strong"
    if score >= MODERATE_SCORE:
        return "Moderate"
    return "Weak"


def freshness_label(tests, penetration, broken):
    if broken:
        return "Broken"
    if tests == 0:
        return "Fresh"
    if tests == 1 and penetration < 0.5:
        return "Tested"
    return "Weakening"


def lifecycle(kind, top, bottom, highs, lows, closes):
    """Tests, deepest penetration (0-1 of the width) and break on later bars."""
    if len(closes) == 0:
        return {"tests": 0, "penetration": 0.0, "broken": False}
    width = max(top - bottom, 1e-9)
    if kind == "demand":
        broken_mask = closes < bottom
        inside = lows <= top
        depth = (top - lows) / width
    else:
        broken_mask = closes > top
        inside = highs >= bottom
        depth = (highs - bottom) / width
    broken = bool(broken_mask.any())
    end = int(np.argmax(broken_mask)) if broken else len(closes)
    inside = inside[:end]
    entries = int(inside[0]) + int(np.count_nonzero(inside[1:] & ~inside[:-1])) if len(inside) else 0
    deepest = float(np.clip(depth[:end][inside], 0, 1).max()) if inside.any() else 0.0
    return {"tests": entries, "penetration": deepest, "broken": broken}


def score_zone(departure_atr, volume_ratio, bos, base_candles, rejection, freshness, tests):
    parts = {
        "departure": round(min(30.0, departure_atr / 4.0 * 30.0), 1),
        "volume": 0 if volume_ratio is None else (15 if volume_ratio >= 1.5 else 8 if volume_ratio >= 1.15 else 0),
        "bos": 15 if bos else 0,
        "base": 8 if rejection else (10 if base_candles <= 3 else 6),
        "freshness": {"Fresh": 20, "Tested": 12, "Weakening": 4}.get(freshness, 0),
        "reactions": min(10, 4 * tests) if freshness != "Broken" else 0,
    }
    return int(round(min(100.0, sum(parts.values())))), parts


def _base_candle(high, low, open_, close, atr):
    span = high - low
    body = abs(close - open_)
    return span <= 0.9 * atr or (span <= 1.3 * atr and body <= 0.5 * max(span, 1e-9))


def _impulsive(high, low, open_, close, atr):
    span = high - low
    return span > 0.9 * atr and abs(close - open_) >= 0.5 * span


def _rejection(kind, high, low, open_, close):
    span = high - low
    if span <= 0:
        return False
    body = abs(close - open_)
    wick = (min(open_, close) - low) if kind == "demand" else (high - max(open_, close))
    return wick >= 2 * body and wick >= 0.5 * span


def detect(frame, timeframe="1d"):
    """Every structural zone in ``frame`` with its lifecycle as of the last bar."""
    frame = clean_frame(frame)
    n = len(frame)
    if n < ATR_PERIOD + 6:
        return []
    o = frame["Open"].to_numpy(float)
    h = frame["High"].to_numpy(float)
    lo = frame["Low"].to_numpy(float)
    c = frame["Close"].to_numpy(float)
    v = frame["Volume"].to_numpy(float)
    atr = atr_series(frame).to_numpy(float)
    has_volume = bool(np.nansum(v) > 0)
    found = []
    start = 2
    while start < n - 2:
        start += 1
        ref_atr = atr[start - 1]
        if not math.isfinite(ref_atr) or ref_atr <= 0:
            continue
        best = None
        for size in range(1, MAX_BASE + 1):
            end = start + size - 1
            if end + 1 >= n:
                break
            if not all(_base_candle(h[i], lo[i], o[i], c[i], ref_atr) for i in range(start, end + 1)):
                rejection_kind = None
                if size == 1:
                    for kind in ("demand", "supply"):
                        if _rejection(kind, h[start], lo[start], o[start], c[start]):
                            rejection_kind = kind
                if not rejection_kind:
                    break
            else:
                rejection_kind = None
            base_high = float(h[start:end + 1].max())
            base_low = float(lo[start:end + 1].min())
            if not rejection_kind and base_high - base_low > 1.5 * ref_atr:
                break
            leg = slice(end + 1, min(n, end + 1 + DEPARTURE_BARS))
            if not _impulsive(h[end + 1], lo[end + 1], o[end + 1], c[end + 1], ref_atr):
                if rejection_kind:
                    break
                continue
            for kind in ("demand", "supply"):
                if rejection_kind and kind != rejection_kind:
                    continue
                if kind == "demand":
                    if c[end + 1] <= o[end + 1]:
                        continue
                    accepted = np.nonzero(c[leg] - base_high >= MIN_DEPARTURE_ATR * ref_atr)[0]
                    if not len(accepted):
                        continue
                    formed = end + 1 + int(accepted[0])
                    departure = (float(h[end + 1:formed + 1].max()) - base_high) / ref_atr
                else:
                    if c[end + 1] >= o[end + 1]:
                        continue
                    accepted = np.nonzero(base_low - c[leg] >= MIN_DEPARTURE_ATR * ref_atr)[0]
                    if not len(accepted):
                        continue
                    formed = end + 1 + int(accepted[0])
                    departure = (base_low - float(lo[end + 1:formed + 1].min())) / ref_atr
                if best is None or departure > best["departure"]:
                    best = {"kind": kind, "start": start, "end": end, "formed": formed,
                            "departure": departure, "rejection": bool(rejection_kind),
                            "base_high": base_high, "base_low": base_low}
            if rejection_kind:
                break
        if best:
            found.append(_build_zone(best, timeframe, frame, o, h, lo, c, v, has_volume))
            start = best["end"]
    return _dedupe(found)


def _build_zone(info, timeframe, frame, o, h, lo, c, v, has_volume):
    kind, start, end, formed = info["kind"], info["start"], info["end"], info["formed"]
    bodies_top = np.maximum(o[start:end + 1], c[start:end + 1])
    bodies_bottom = np.minimum(o[start:end + 1], c[start:end + 1])
    if kind == "demand":
        bottom = info["base_low"]
        top = float(bodies_top.max())
        if top <= bottom:
            top = info["base_high"]
    else:
        top = info["base_high"]
        bottom = float(bodies_bottom.min())
        if bottom >= top:
            bottom = info["base_low"]
    leg_in = c[start - 1] - o[start - 3]
    leg_in_name = "Rally" if leg_in > 0 else "Drop"
    leg_out_name = "Rally" if kind == "demand" else "Drop"
    if info["rejection"]:
        pattern, pattern_name = "REJ", f"{'Bullish' if kind == 'demand' else 'Bearish'} rejection wick"
    else:
        pattern = f"{leg_in_name[0]}B{leg_out_name[0]}"
        pattern_name = f"{leg_in_name}-Base-{leg_out_name}"
    prior = slice(max(0, start - 10), start)
    if kind == "demand":
        bos = bool(start - prior.start >= 3 and h[end + 1:formed + 1].max() > h[prior].max())
    else:
        bos = bool(start - prior.start >= 3 and lo[end + 1:formed + 1].min() < lo[prior].min())
    volume_ratio = None
    if has_volume:
        before = v[max(0, start - 20):start]
        before = before[before > 0]
        after = v[end + 1:formed + 1]
        if len(before) >= 5 and after.size and after.mean() > 0:
            volume_ratio = round(float(after.mean() / before.mean()), 2)
    life = lifecycle(kind, top, bottom, h[formed + 1:], lo[formed + 1:], c[formed + 1:])
    freshness = freshness_label(life["tests"], life["penetration"], life["broken"])
    score, parts = score_zone(info["departure"], volume_ratio, bos, end - start + 1,
                              info["rejection"], freshness, life["tests"])
    index = frame.index
    return {
        "type": kind,
        "timeframe": timeframe,
        "top": round(float(top), 2),
        "bottom": round(float(bottom), 2),
        "time": _epoch(_period_start(index[start], timeframe)),
        "formed_time": _epoch(index[formed]),
        "pattern": pattern,
        "pattern_name": pattern_name,
        "base_candles": int(end - start + 1),
        "departure_atr": round(float(info["departure"]), 2),
        "volume_ratio": volume_ratio,
        "volume_confirmed": None if volume_ratio is None else volume_ratio >= 1.15,
        "bos": bos,
        "tests": life["tests"],
        "penetration": round(life["penetration"], 2),
        "broken": life["broken"],
        "freshness": freshness,
        "score": score,
        "score_parts": parts,
        "strength": strength_label(score),
    }


def _overlaps(a, b):
    return a["bottom"] <= b["top"] and b["bottom"] <= a["top"]


def _dedupe(zones):
    """One zone per overlapping cluster of the same type: the highest score, then the newest."""
    kept = []
    for zone in sorted(zones, key=lambda item: (item["broken"], -item["score"], -item["time"])):
        if any(other["type"] == zone["type"] and _overlaps(zone, other) for other in kept):
            continue
        kept.append(zone)
    return sorted(kept, key=lambda item: item["time"])


def is_valid(zone):
    return (not zone["broken"] and zone["score"] >= MIN_SCORE
            and zone["departure_atr"] >= MIN_DEPARTURE_ATR)


def distance_pct(zone, price):
    if not price:
        return None
    if zone["bottom"] <= price <= zone["top"]:
        return 0.0
    if price > zone["top"]:
        return round((price - zone["top"]) / price * 100, 2)
    return round((zone["bottom"] - price) / price * 100, 2)


def correct_side(zone, price):
    if zone["type"] == "demand":
        return zone["bottom"] <= price
    return zone["top"] >= price


def distance_limits(daily, price):
    atr = atr_series(daily).iloc[-1] if len(daily) > ATR_PERIOD else float("nan")
    atr_pct = float(atr) / price * 100 if price and math.isfinite(float(atr)) else None
    limits = {}
    for timeframe, (multiple, floor, cap) in DISTANCE_RULES.items():
        limits[timeframe] = floor if atr_pct is None else round(min(cap, max(floor, multiple * atr_pct)), 2)
    return (None if atr_pct is None else round(atr_pct, 2)), limits


def reasons(zone):
    label = f"{LABELS.get(zone['timeframe'], zone['timeframe'])} {zone['type']}"
    if zone["pattern"] == "REJ":
        base = f"{zone['pattern_name']} followed by a {zone['departure_atr']:.1f}x ATR departure"
    else:
        base = (f"{zone['pattern_name']}: {zone['base_candles']}-candle base, then a "
                f"{zone['departure_atr']:.1f}x ATR impulsive departure")
    rows = [f"{label}: {base}"]
    rows.append("Break of structure: the departure cleared the prior 10-bar "
                + ("high" if zone["type"] == "demand" else "low") if zone["bos"]
                else "No break of structure on the departure")
    if zone["volume_ratio"] is None:
        rows.append("Volume confirmation unavailable (no volume data)")
    else:
        rows.append(f"Departure volume {zone['volume_ratio']:.2f}x the prior 20-bar average"
                    + (" (confirmed)" if zone["volume_confirmed"] else " (not confirmed)"))
    if zone["tests"] == 0:
        rows.append("Fresh: price has not returned to the zone since it formed")
    else:
        rows.append(f"Tested {zone['tests']} time(s); no close beyond the far edge")
    rows.append(f"Strength score {zone['score']}/100 (minimum {MIN_SCORE} to display)")
    return rows


def _public(zone, price):
    item = dict(zone)
    item["distance_pct"] = distance_pct(zone, price)
    item["fresh"] = zone["freshness"] == "Fresh"
    item["status"] = zone["freshness"]
    item["grade"] = zone["strength"]
    item["label"] = f"{LABELS.get(zone['timeframe'], zone['timeframe'])} {zone['type'].title()}"
    item["reasons"] = reasons(zone)
    item.pop("score_parts", None)
    return item


def select_nearby(zones, price, limit_pct, per_side=PER_SIDE):
    """Nearest valid demand at/below price and supply at/above price within the limit."""
    picked = {"demand": [], "supply": []}
    for zone in zones:
        if not is_valid(zone) or not correct_side(zone, price):
            continue
        gap = distance_pct(zone, price)
        if gap is None or gap > limit_pct:
            continue
        picked[zone["type"]].append(zone)
    for kind in picked:
        picked[kind].sort(key=lambda zone: (distance_pct(zone, price),
                                            -zone["score"] * TIMEFRAME_WEIGHT.get(zone["timeframe"], 1.0)))
        picked[kind] = picked[kind][:per_side]
    return picked


def merge_confluence(zones):
    """Overlapping zones of one type from different timeframes become one zone."""
    order = {name: index for index, name in enumerate(TIMEFRAMES)}
    groups = []
    for zone in sorted(zones, key=lambda item: order.get(item["timeframe"], 9)):
        for group in groups:
            if group[0]["type"] == zone["type"] and any(_overlaps(zone, member) for member in group) \
                    and zone["timeframe"] not in {member["timeframe"] for member in group}:
                group.append(zone)
                break
        else:
            groups.append([zone])
    merged = []
    for group in groups:
        if len(group) == 1:
            item = dict(group[0])
            item["timeframes"] = [item["timeframe"]]
            item["confluence"] = False
            merged.append(item)
            continue
        lead = max(group, key=lambda member: member["score"] * TIMEFRAME_WEIGHT.get(member["timeframe"], 1.0))
        item = dict(lead)
        frames = sorted({member["timeframe"] for member in group}, key=lambda name: order.get(name, 9))
        names = " + ".join(LABELS.get(name, name) for name in frames)
        item.update({
            "top": max(member["top"] for member in group),
            "bottom": min(member["bottom"] for member in group),
            "time": min(member["time"] for member in group),
            "timeframes": frames,
            "confluence": True,
            "label": f"{names} {lead['type'].title()} Confluence",
            "score": min(100, lead["score"] + 5 * (len(group) - 1)),
            "tests": max(member["tests"] for member in group),
            "members": [{key: member.get(key) for key in (
                "timeframe", "top", "bottom", "score", "strength", "freshness", "tests", "pattern_name")}
                for member in group],
        })
        item["strength"] = item["grade"] = strength_label(item["score"])
        item["reasons"] = [f"Confluence of {names} {lead['type']} zones"] + [
            row for member in group for row in member["reasons"][:1]] + lead["reasons"][1:]
        merged.append(item)
    return merged


def analyze(daily_df, timeframes=TIMEFRAMES, price=None, per_side=PER_SIDE):
    """Nearby zones per timeframe plus the merged chart list, as of the last daily bar."""
    daily = clean_frame(daily_df)
    requested = tuple(timeframes) or TIMEFRAMES
    empty = {name: {"demand": None, "supply": None} for name in TIMEFRAMES}
    result = {"price": None, "atr_pct": None, "limits": {}, "nearby": {}, "nearest": empty,
              "display": [], "messages": {"demand": NO_DEMAND, "supply": NO_SUPPLY}}
    if len(daily) < ATR_PERIOD + 6:
        return result
    price = float(price if price is not None else daily["Close"].iloc[-1])
    atr_pct, limits = distance_limits(daily, price)
    result.update({"price": round(price, 2), "atr_pct": atr_pct, "limits": limits})
    chosen = []
    nearest = dict(empty)
    for timeframe in requested:
        if timeframe not in TIMEFRAMES:
            continue
        try:
            zones = detect(timeframe_frame(daily, timeframe), timeframe)
        except Exception:
            zones = []
        picked = select_nearby(zones, price, limits[timeframe], per_side)
        public = {kind: [_public(zone, price) for zone in rows] for kind, rows in picked.items()}
        result["nearby"][timeframe] = public
        nearest[timeframe] = {kind: (rows[0] if rows else None) for kind, rows in public.items()}
        chosen.extend(public["demand"] + public["supply"])
    result["nearest"] = nearest
    display = merge_confluence(chosen)
    for zone in display:
        zone["distance_pct"] = distance_pct(zone, price)
    display.sort(key=lambda zone: (zone["distance_pct"], zone["type"]))
    result["display"] = display
    for kind, message in (("demand", NO_DEMAND), ("supply", NO_SUPPLY)):
        result["messages"][kind] = None if any(zone["type"] == kind for zone in display) else message
    return result


def nearest_map(daily_df, timeframes=TIMEFRAMES):
    """{timeframe: {"demand": zone|None, "supply": zone|None}} for every timeframe."""
    return analyze(daily_df, timeframes)["nearest"]


def frame_nearest(frame, key):
    """Nearest zones on the given bars as they are (intraday frames)."""
    bars = clean_frame(frame)
    if len(bars) < ATR_PERIOD + 6:
        return {"demand": None, "supply": None}
    price = float(bars["Close"].iloc[-1])
    zones = detect(bars, key)
    _, limits = distance_limits(bars, price)
    picked = select_nearby(zones, price, limits["1d"], 1)
    return {kind: (_public(rows[0], price) if rows else None) for kind, rows in picked.items()}


ZONE_COLORS = {
    "demand": ("#16a34a", "rgba(22,163,74,0.16)"),
    "supply": ("#dc2626", "rgba(220,38,38,0.16)"),
}


def chart_rectangles(display, display_index, bars=DISPLAY_BARS):
    """Chart-ready rectangles that start near the latest candles, not at the left edge."""
    index = pd.DatetimeIndex(display_index)
    if index.tz is not None:
        index = index.tz_convert("Asia/Kolkata").tz_localize(None)
    if len(index) == 0:
        return []
    window_start = index[max(0, len(index) - bars)]
    rows = []
    for zone in display:
        formed = pd.Timestamp(zone["time"], unit="s")
        later = index[index >= formed]
        origin = later[0] if len(later) else index[-1]
        start = max(origin, window_start)
        color, fill = ZONE_COLORS[zone["type"]]
        item = dict(zone)
        item.update({
            "start_time": _epoch(start),
            "time": _epoch(origin),
            "end_time": _epoch(index[-1]),
            "band": False,
            "color": color,
            "fill": fill,
        })
        rows.append(item)
    return rows
