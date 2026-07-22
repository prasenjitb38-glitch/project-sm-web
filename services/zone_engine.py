"""Supply and demand zone detection based on Rally/Base/Drop structures."""

import pandas as pd


TIMEFRAME_RULES = {
    "1d": None,
    "1wk": "W-FRI",
    "1mo": "ME",
    "3mo": "QE",
    "6mo": "2QE",
    "1y": "YE",
    "5y": "5YE",
}

# Used only for quality confirmation; detected zones keep their own timeframe.
HIGHER_TIMEFRAME = {
    "1d": "1wk", "1wk": "1mo", "1mo": "3mo", "3mo": "6mo",
    "6mo": "1y", "1y": "5y", "5y": None,
}


def for_timeframe(df, timeframe):
    """Convert daily OHLC data to the requested chart timeframe."""
    rule = TIMEFRAME_RULES.get(timeframe)
    if not rule:
        return df.copy()
    return df.resample(rule).agg({
        "Open": "first", "High": "max", "Low": "min",
        "Close": "last", "Volume": "sum",
    }).dropna()


def _timestamp(value):
    return int(pd.Timestamp(value).tz_localize(None).timestamp())


def _pattern_name(prior, base_size, departure):
    names = {"R": "Rally", "D": "Drop"}
    return " -> ".join([names[prior]] + ["Base"] * base_size + [names[departure]])


def _higher_timeframe_confirmation(source, timeframe, timestamp, zone_type):
    """Confirm that the broader timeframe moves in the zone's direction."""
    higher_timeframe = HIGHER_TIMEFRAME.get(timeframe)
    if not higher_timeframe:
        return False, None
    higher = for_timeframe(source, higher_timeframe)
    recent = higher.loc[:timestamp].tail(3)
    if len(recent) < 3:
        return False, higher_timeframe
    movement = float(recent["Close"].iloc[-1] - recent["Open"].iloc[0])
    return (movement >= 0 if zone_type == "demand" else movement <= 0), higher_timeframe


def detect_zones(df, timeframe="1d", max_zones=4):
    """Find strong RBR/DBR demand and DBD/RBD supply zones.

    A base is one to three compact candles. A zone is strong only when its next
    three candles create a large ATR-confirmed departure. A later touch marks the
    zone tested; a break through the distal line excludes it completely.
    """
    source = df.copy()
    work = for_timeframe(source, timeframe).copy()
    if len(work) < 18:
        return []

    work["range"] = work["High"] - work["Low"]
    work["atr"] = work["range"].rolling(14, min_periods=6).mean()
    zones = []

    for start in range(6, len(work) - 4):
        atr = float(work["atr"].iloc[start] or 0)
        if atr <= 0:
            continue

        for base_size in range(1, 4):
            end = start + base_size - 1
            if end + 3 >= len(work):
                continue
            base = work.iloc[start:end + 1]
            base_high, base_low = float(base["High"].max()), float(base["Low"].min())
            base_range = base_high - base_low
            if base_range > atr * 1.35 or float(base["range"].max()) > atr * 0.95:
                continue

            before = work.iloc[start - 3:start]
            after = work.iloc[end + 1:end + 4]
            later = work.iloc[end + 4:]
            prior_move = float(before["Close"].iloc[-1] - before["Open"].iloc[0])
            up_departure = float(after["High"].max() - base_high)
            down_departure = float(base_low - after["Low"].min())
            minimum_departure = max(atr * 1.15, base_range * 1.4)
            previous_high = float(before["High"].max())
            previous_low = float(before["Low"].min())
            average_volume = 0.0
            departure_volume = 0.0
            if "Volume" in work.columns:
                volume_window = work["Volume"].iloc[max(0, start - 14):start]
                volume_mean = volume_window.replace(0, pd.NA).mean()
                departure_mean = after["Volume"].mean()
                average_volume = float(volume_mean) if pd.notna(volume_mean) else 0.0
                departure_volume = float(departure_mean) if pd.notna(departure_mean) else 0.0
            volume_ratio = departure_volume / average_volume if average_volume > 0 else 1.0

            zone = None
            if up_departure >= minimum_departure and float(after["High"].max()) > previous_high:
                prior = "R" if prior_move >= atr * 0.25 else "D"
                pattern = f"{prior}{'B' * base_size}R"
                # A valid demand zone must not be broken below its distal line.
                if not later.empty and float(later["Low"].min()) < base_low:
                    continue
                tested = not later.empty and float(later["Low"].min()) <= base_high
                zone = {
                    "pattern": pattern, "pattern_name": _pattern_name(prior, base_size, "R"),
                    "type": "demand", "time": _timestamp(base.index[0]),
                    "top": round(base_high, 2), "bottom": round(base_low, 2),
                    "entry_low": round(base_low, 2), "entry_high": round(base_high, 2),
                    "exit": round(float(after["High"].max()), 2), "tested": tested,
                }
            elif down_departure >= minimum_departure and float(after["Low"].min()) < previous_low:
                prior = "D" if prior_move <= -atr * 0.25 else "R"
                pattern = f"{prior}{'B' * base_size}D"
                # A valid supply zone must not be broken above its distal line.
                if not later.empty and float(later["High"].max()) > base_high:
                    continue
                tested = not later.empty and float(later["High"].max()) >= base_low
                zone = {
                    "pattern": pattern, "pattern_name": _pattern_name(prior, base_size, "D"),
                    "type": "supply", "time": _timestamp(base.index[0]),
                    "top": round(base_high, 2), "bottom": round(base_low, 2),
                    "entry_low": round(base_low, 2), "entry_high": round(base_high, 2),
                    "exit": round(float(after["Low"].min()), 2), "tested": tested,
                }

            if zone:
                impulse = up_departure if zone["type"] == "demand" else down_departure
                risk = base_range
                reward = (
                    float(zone["exit"]) - base_high
                    if zone["type"] == "demand"
                    else base_low - float(zone["exit"])
                )
                risk_reward = reward / risk if risk > 0 else 0.0

                # Project SM 100-point scoring system:
                # Pattern 20, Fresh 20, Departure 15, BOS 15, Volume 10,
                # Higher timeframe 10, ATR width 5 and Risk:Reward 5.
                width_ratio = base_range / atr
                departure_score = min(15, max(0, (impulse / atr - 1.15) / 0.85 * 15))
                width_score = min(5, max(0, (1.35 - width_ratio) / 0.85 * 5))
                volume_score = min(10, max(0, (volume_ratio - 1.0) / 0.5 * 10))
                freshness_score = 20 if not zone["tested"] else 0
                htf_confirmed, higher_timeframe = _higher_timeframe_confirmation(
                    source, timeframe, base.index[-1], zone["type"]
                )
                htf_score = 10 if htf_confirmed else 0
                rr_score = 5 if risk_reward >= 2 else 0
                zone["fresh"] = not zone["tested"]
                zone["swing_break"] = True
                zone["volume_ratio"] = round(volume_ratio, 2)
                zone["higher_timeframe"] = (higher_timeframe or "N/A").upper()
                zone["higher_timeframe_confirmed"] = htf_confirmed
                zone["trend_aligned"] = htf_confirmed
                zone["risk_reward"] = round(risk_reward, 2)
                zone["rr_ok"] = risk_reward >= 2
                zone["score"] = min(100, round(
                    20 + freshness_score + departure_score + 15 + volume_score
                    + htf_score + width_score + rr_score
                ))
                if zone["score"] >= 90:
                    zone["grade"] = "Premium Zone"
                    zone["stars"] = "★★★★★"
                elif zone["score"] >= 80:
                    zone["grade"] = "Strong Zone"
                    zone["stars"] = "★★★★"
                elif zone["score"] >= 70:
                    zone["grade"] = "Good Zone"
                    zone["stars"] = "★★★"
                else:
                    zone["grade"] = "Below Filter"
                    zone["stars"] = ""
                zone["timeframe"] = timeframe.upper()
                zones.append(zone)

    # Prefer recent, non-overlapping zones of each kind.
    selected = []
    for zone in sorted(zones, key=lambda item: item["time"], reverse=True):
        overlap = any(
            zone["type"] == existing["type"]
            and zone["bottom"] <= existing["top"]
            and zone["top"] >= existing["bottom"]
            for existing in selected
        )
        if not overlap:
            selected.append(zone)
        if len(selected) >= max_zones:
            break
    return sorted(selected, key=lambda item: item["time"])
