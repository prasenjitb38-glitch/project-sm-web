"""Institutional-style supply and demand zone detection for Project SM.

The engine is deliberately rule-based: its score explains *why* a zone was
selected. It is a scanner aid, not investment advice.
"""

import pandas as pd


TIMEFRAME_RULES = {
    "1d": None, "1wk": "W-FRI", "1mo": "ME", "3mo": "QE",
    "6mo": "2QE", "1y": "YE", "5y": "5YE",
}
HIGHER_TIMEFRAME = {
    "1d": "1wk", "1wk": "1mo", "1mo": "3mo", "3mo": "6mo",
    "6mo": "1y", "1y": "5y", "5y": None,
}
TIMEFRAME_LABELS = {
    "1d": "Dly", "1wk": "Wly", "1mo": "Mly", "3mo": "Qly",
    "6mo": "Hly", "1y": "Yrly", "5y": "5Yr",
}


def for_timeframe(df, timeframe):
    """Convert daily OHLCV candles to a Project SM timeframe."""
    rule = TIMEFRAME_RULES.get(timeframe)
    if not rule:
        return df.copy()
    return df.resample(rule).agg({
        "Open": "first", "High": "max", "Low": "min",
        "Close": "last", "Volume": "sum",
    }).dropna()


def _timestamp(value):
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is not None:
        stamp = stamp.tz_localize(None)
    return int(stamp.timestamp())


def _indicators(frame):
    work = frame.copy()
    work["range"] = work["High"] - work["Low"]
    previous_close = work["Close"].shift(1)
    true_range = pd.concat([
        work["range"], (work["High"] - previous_close).abs(),
        (work["Low"] - previous_close).abs(),
    ], axis=1).max(axis=1)
    work["atr"] = true_range.rolling(14, min_periods=8).mean()
    work["ema20"] = work["Close"].ewm(span=20, adjust=False).mean()
    work["ema50"] = work["Close"].ewm(span=50, adjust=False).mean()
    work["ema200"] = work["Close"].ewm(span=200, adjust=False).mean()
    change = work["Close"].diff()
    gain = change.clip(lower=0).rolling(14, min_periods=8).mean()
    loss = (-change.clip(upper=0)).rolling(14, min_periods=8).mean()
    rs = gain / loss.replace(0, pd.NA)
    work["rsi"] = 100 - (100 / (1 + rs))
    if "Volume" not in work:
        work["Volume"] = 0
    work["volume_average"] = work["Volume"].replace(0, pd.NA).rolling(20, min_periods=5).mean()
    return work


def _direction(candles):
    """Classify the pre-base leg as Rally or Drop."""
    return "R" if float(candles["Close"].iloc[-1] - candles["Open"].iloc[0]) >= 0 else "D"


def _pattern_name(prior, base_size, departure):
    words = {"R": "Rally", "D": "Drop"}
    return " -> ".join([words[prior]] + ["Base"] * base_size + [words[departure]])


def _higher_timeframe_confirmation(source, timeframe, timestamp, zone_type):
    """Return every aligned broader timeframe, e.g. ``Wly + Mly + Hly``."""
    candidates = []
    next_timeframe = HIGHER_TIMEFRAME.get(timeframe)
    while next_timeframe:
        candidates.append(next_timeframe)
        next_timeframe = HIGHER_TIMEFRAME.get(next_timeframe)
    confirmations = []
    for higher_name in candidates:
        higher = _indicators(for_timeframe(source, higher_name))
        recent = higher.loc[:timestamp].tail(3)
        if len(recent) < 3:
            continue
        close_now = float(recent["Close"].iloc[-1])
        close_then = float(recent["Close"].iloc[0])
        aligned = close_now >= close_then if zone_type == "demand" else close_now <= close_then
        if aligned:
            confirmations.append(TIMEFRAME_LABELS[higher_name])
    return bool(confirmations), " + ".join(confirmations) if confirmations else "No HTF support"


def _grade(score):
    if score >= 90:
        return "Institutional Strong Zone", "★★★★★"
    if score >= 75:
        return "Strong Zone", "★★★★☆"
    if score >= 60:
        return "Medium Zone", "★★★☆☆"
    return "Weak Zone", "★★☆☆☆"


def detect_zones(df, timeframe="1d", max_zones=4):
    """Find RBR/DBR demand and DBD/RBD supply zones using the agreed rules.

    Requirements: 2–6 compact base candles, 3× ATR departure, high-volume
    breakout, untested zone, fair-value-gap style imbalance, trend/RSI/EMA
    confirmation and higher-timeframe support or resistance.
    """
    source = df.copy().sort_index()
    work = _indicators(for_timeframe(source, timeframe))
    if len(work) < 35:
        return []
    zones = []

    for start in range(8, len(work) - 4):
        atr = work["atr"].iloc[start]
        if pd.isna(atr) or float(atr) <= 0:
            continue
        atr = float(atr)
        prior_candles = work.iloc[start - 4:start]

        for base_size in range(2, 7):
            end = start + base_size - 1
            if end + 3 >= len(work):
                continue
            base = work.iloc[start:end + 1]
            after = work.iloc[end + 1:end + 4]
            later = work.iloc[end + 4:]
            base_high, base_low = float(base["High"].max()), float(base["Low"].min())
            base_width = base_high - base_low

            # A valid base is compact compared with its recent ATR.
            if base_width > atr * 1.6 or float(base["range"].max()) > atr * 1.05:
                continue

            prior = _direction(prior_candles)
            previous_high, previous_low = float(prior_candles["High"].max()), float(prior_candles["Low"].min())
            up_move = float(after["High"].max() - base_high)
            down_move = float(base_low - after["Low"].min())
            departure_volume = float(after["Volume"].mean() or 0)
            base_volume = work["volume_average"].iloc[start]
            volume_ratio = departure_volume / float(base_volume) if pd.notna(base_volume) and float(base_volume) > 0 else 1.0

            zone_type = None
            impulse = 0.0
            fvg = False
            bos = False
            fresh = False
            liquidity_sweep = False

            # Demand: RBR or DBR with at least 3 ATR upward departure.
            if up_move >= atr * 3 and float(after["Close"].iloc[-1]) > base_high:
                zone_type, impulse = "demand", up_move
                bos = float(after["High"].max()) > previous_high
                # Bullish FVG / imbalance: third candle low stays above first candle high.
                fvg = float(after["Low"].iloc[-1]) > float(after["High"].iloc[0])
                fresh = later.empty or float(later["Low"].min()) > base_high
                liquidity_sweep = float(prior_candles["Low"].min()) < float(work["Low"].iloc[start - 5:start].min())

            # Supply: DBD or RBD with at least 3 ATR downward departure.
            elif down_move >= atr * 3 and float(after["Close"].iloc[-1]) < base_low:
                zone_type, impulse = "supply", down_move
                bos = float(after["Low"].min()) < previous_low
                # Bearish FVG / imbalance: third candle high stays below first candle low.
                fvg = float(after["High"].iloc[-1]) < float(after["Low"].iloc[0])
                fresh = later.empty or float(later["High"].max()) < base_low
                liquidity_sweep = float(prior_candles["High"].max()) > float(work["High"].iloc[start - 5:start].max())

            if not zone_type:
                continue

            last_base = base.iloc[-1]
            rsi = last_base["rsi"]
            rsi_ok = False
            if pd.notna(rsi) and 40 <= float(rsi) <= 60:
                rsi_change = float(work["rsi"].iloc[end] - work["rsi"].iloc[max(0, end - 2)])
                rsi_ok = rsi_change >= 0 if zone_type == "demand" else rsi_change <= 0
            ema20, ema50, ema200 = (float(last_base[key]) for key in ("ema20", "ema50", "ema200"))
            ema_ok = ema20 > ema50 > ema200 if zone_type == "demand" else ema20 < ema50 < ema200
            trend_ok = ema_ok and rsi_ok
            htf_ok, htf_name = _higher_timeframe_confirmation(source, timeframe, base.index[-1], zone_type)

            # Score: Fresh 20, departure 20, volume 15, HTF 15, base 10,
            # ATR expansion 10, trend 5 and liquidity sweep 5.
            score = (
                (20 if fresh else 0)
                + min(20, round((impulse / atr) / 4 * 20))
                + (15 if volume_ratio >= 1.5 else 8 if volume_ratio >= 1.15 else 0)
                + (15 if htf_ok else 0)
                + (10 if 2 <= base_size <= 4 else 7)
                + (10 if impulse >= 3 * atr and base_width <= 1.2 * atr else 5)
                + (5 if trend_ok else 0)
                + (5 if liquidity_sweep else 0)
            )
            # BOS and FVG are compulsory quality confirmations, rather than
            # hidden score points. This stops a score from hiding weak structure.
            if not (bos and fvg):
                score = min(score, 74)
            score = min(100, int(score))
            grade, stars = _grade(score)
            reward = float(after["High"].max()) - base_high if zone_type == "demand" else base_low - float(after["Low"].min())
            risk_reward = reward / base_width if base_width else 0

            zones.append({
                "pattern": f"{prior}{'B' * base_size}{'R' if zone_type == 'demand' else 'D'}",
                "pattern_name": _pattern_name(prior, base_size, "R" if zone_type == "demand" else "D"),
                "type": zone_type, "time": _timestamp(base.index[0]),
                "top": round(base_high, 2), "bottom": round(base_low, 2),
                "entry_low": round(base_low, 2), "entry_high": round(base_high, 2),
                "exit": round(float(after["High"].max() if zone_type == "demand" else after["Low"].min()), 2),
                "fresh": fresh, "tested": not fresh, "score": score,
                "grade": grade, "stars": stars, "timeframe": timeframe.upper(),
                "base_candles": base_size, "departure_atr": round(impulse / atr, 2),
                "volume_ratio": round(volume_ratio, 2), "fvg": fvg, "bos": bos,
                "liquidity_sweep": liquidity_sweep, "trend_aligned": trend_ok,
                "rsi_confirmation": rsi_ok, "ema_confirmation": ema_ok,
                "higher_timeframe": htf_name, "higher_timeframe_confirmed": htf_ok,
                "risk_reward": round(risk_reward, 2),
            })

    # Keep newest non-overlapping zones. Fresh higher-score candidates win.
    selected = []
    for zone in sorted(zones, key=lambda item: (item["fresh"], item["score"], item["time"]), reverse=True):
        overlap = any(
            zone["type"] == current["type"]
            and zone["bottom"] <= current["top"] and zone["top"] >= current["bottom"]
            for current in selected
        )
        if not overlap:
            selected.append(zone)
        if len(selected) >= max_zones:
            break
    return sorted(selected, key=lambda item: item["time"])
