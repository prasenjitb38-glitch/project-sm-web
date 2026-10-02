"""Trading intelligence built only from calculated indicators, zones, and signals.

A missing input is reported as unavailable. It is not replaced with another
timeframe or a guessed pattern.
"""

import pandas as pd

from services.signal_engine import build_signal_from_enriched
from services.technical_analysis import (
    _last_patterns,
    _num,
    _snapshot_values,
    _zone_nearby,
    add_indicators,
    prepare_ohlcv,
    resample_chart,
    trend_from_frame,
)

GROUP_MEMBERS = {
    "Trend": ("EMA alignment", "Price vs EMA 20", "EMA 20 vs EMA 50", "ADX confirmation", "Supertrend"),
    "Momentum": ("RSI", "MACD"),
    "Volume": ("Relative volume", "Session VWAP"),
    "Price action": ("Price action",),
    "Demand and supply": ("Demand and supply",),
}


def quote_stats(daily_df):
    """Last daily session and the high/low of the latest 252 daily bars."""
    frame = prepare_ohlcv(daily_df)
    if frame.empty or _num(frame["Close"].iloc[-1]) is None:
        return {"data_unavailable": True, "message": "Data Unavailable"}
    row = frame.iloc[-1]
    previous = frame["Close"].iloc[-2] if len(frame) > 1 else None
    price = _num(row["Close"])
    prev_close = _num(previous)
    change = None if price is None or prev_close is None else _num(price - prev_close)
    change_pct = None if price is None or not prev_close else _num((price - prev_close) / prev_close * 100)
    window = frame.tail(252)
    return {
        "open": _num(row["Open"]),
        "high": _num(row["High"]),
        "low": _num(row["Low"]),
        "previous_close": prev_close,
        "volume": _num(row["Volume"], 0),
        "change": change,
        "change_pct": change_pct,
        "week52_high": _num(window["High"].max()),
        "week52_low": _num(window["Low"].min()),
        "week52_basis": "high and low of the last 252 daily bars",
        "market_cap": None,
        "market_cap_status": "Data Unavailable",
        "exchange": "NSE",
    }


def _component_map(signal):
    return {row.get("name"): row for row in (signal or {}).get("components") or []}


def _state(components, name):
    row = components.get(name) or {}
    return row.get("state"), row.get("detail"), row.get("points")


def quality_scores(signal, mtf_rows):
    """Split the existing +10/−10 rules into groups. Unavailable rules stay out."""
    components = _component_map(signal)
    groups = []
    net = 0
    maximum = 0
    for name, members in GROUP_MEMBERS.items():
        rows = [components[item] for item in members if item in components and components[item].get("points") is not None]
        group_net = sum(row["points"] for row in rows)
        group_max = 10 * len(rows)
        net += group_net
        maximum += group_max
        groups.append({
            "name": name,
            "points": group_net,
            "maximum": group_max,
            "score": None if not group_max else int(round(abs(group_net) / group_max * 100)),
            "direction": "bullish" if group_net > 0 else "bearish" if group_net < 0 else "neutral" if rows else "unavailable",
            "reasons": [row["detail"] for row in rows if row.get("points")],
        })
    mtf_points = []
    mtf_reasons = []
    for row in mtf_rows or []:
        status = row.get("status")
        if status == "Bullish":
            mtf_points.append(10)
            mtf_reasons.append(f"{row.get('label')} trend is bullish")
        elif status == "Bearish":
            mtf_points.append(-10)
            mtf_reasons.append(f"{row.get('label')} trend is bearish")
        elif status == "Neutral":
            mtf_points.append(0)
            mtf_reasons.append(f"{row.get('label')} trend is neutral")
    mtf_max = 10 * len(mtf_points)
    mtf_net = sum(mtf_points)
    groups.append({
        "name": "Multi-timeframe",
        "points": mtf_net if mtf_points else None,
        "maximum": mtf_max,
        "score": None if not mtf_max else int(round(abs(mtf_net) / mtf_max * 100)),
        "direction": "unavailable" if not mtf_points else "bullish" if mtf_net > 0 else "bearish" if mtf_net < 0 else "neutral",
        "reasons": mtf_reasons,
    })
    if mtf_points:
        net += mtf_net
        maximum += mtf_max
    return {
        "groups": groups,
        "signal_score": (signal or {}).get("strength"),
        "quality_score": None if not maximum else int(round(abs(net) / maximum * 100)),
        "quality_net": net,
        "quality_maximum": maximum,
        "formula": (
            "Each available rule is +10 or -10. A group score is the absolute group net "
            "divided by 10 times the number of available rules in that group. "
            "The signal score is the existing signal strength and does not add the multi-timeframe group. "
            "The quality score includes the multi-timeframe group. Unavailable rules are excluded."
        ),
    }


def _swing_divergence(frame):
    """Compare the last two 14-bar swing lows and highs. None when RSI is missing."""
    window = frame.tail(28)
    if len(window) < 28 or "rsi" not in window.columns:
        return {"bullish": None, "bearish": None, "detail": "Not enough bars for divergence"}
    older, newer = window.iloc[:14], window.iloc[14:]
    if older["rsi"].isna().all() or newer["rsi"].isna().all():
        return {"bullish": None, "bearish": None, "detail": "RSI is missing"}
    old_low_at = older["Low"].idxmin()
    new_low_at = newer["Low"].idxmin()
    old_high_at = older["High"].idxmax()
    new_high_at = newer["High"].idxmax()
    bullish = float(newer.loc[new_low_at, "Low"]) < float(older.loc[old_low_at, "Low"]) and float(newer.loc[new_low_at, "rsi"]) > float(older.loc[old_low_at, "rsi"])
    bearish = float(newer.loc[new_high_at, "High"]) > float(older.loc[old_high_at, "High"]) and float(newer.loc[new_high_at, "rsi"]) < float(older.loc[old_high_at, "rsi"])
    return {
        "bullish": bullish,
        "bearish": bearish,
        "detail": "Last 14-bar swing versus the prior 14-bar swing",
    }


def detect_setups(frame, signal, zones):
    """Return only setups whose listed reasons are true on this bar."""
    if frame is None or len(frame) < 2:
        return []
    values = _snapshot_values(frame)
    previous = _snapshot_values(frame.iloc[:-1])
    price = values.get("price")
    patterns, _fib = _last_patterns(frame, price)
    components = _component_map(signal)
    demand = (zones or {}).get("demand")
    supply = (zones or {}).get("supply")
    near_demand = _zone_nearby(demand, price, "demand")
    near_supply = _zone_nearby(supply, price, "supply")
    divergence = _swing_divergence(frame)
    found = []

    def add(name, side, reasons):
        clean = [item for item in reasons if item]
        if clean:
            found.append({"name": name, "side": side, "reasons": clean})

    if near_demand and values.get("change_pct") is not None and values["change_pct"] > 0:
        add("Demand Bounce", "BUY", [
            "Price is inside or within 5% of the calculated demand zone",
            f"The last bar change is positive ({values['change_pct']}%)",
        ])
    if near_supply and values.get("change_pct") is not None and values["change_pct"] < 0:
        add("Supply Rejection", "SELL", [
            "Price is inside or within 5% of the calculated supply zone",
            f"The last bar change is negative ({values['change_pct']}%)",
        ])
    alignment, alignment_detail, _points = _state(components, "EMA alignment")
    atr = values.get("atr")
    ema20, ema50 = values.get("ema20"), values.get("ema50")
    if alignment == "bullish" and price is not None and ema20 is not None and ema50 is not None and atr and abs(price - ema20) <= atr and price > ema50:
        add("EMA Pullback", "BUY", [
            alignment_detail,
            f"Close is within one ATR of EMA 20 (distance {abs(price - ema20):.2f}, ATR {atr})",
            "Close is above EMA 50",
        ])
    prev_ema20 = previous.get("ema20")
    prev_close = previous.get("price")
    if price is not None and ema20 is not None and prev_close is not None and prev_ema20 is not None and prev_close <= prev_ema20 and price > ema20:
        add("EMA Breakout", "BUY", ["The prior close was at or below EMA 20", "The current close is above EMA 20"])
    if price is not None and ema20 is not None and prev_close is not None and prev_ema20 is not None and prev_close >= prev_ema20 and price < ema20:
        add("EMA Breakdown", "SELL", ["The prior close was at or above EMA 20", "The current close is below EMA 20"])
    if patterns.get("breakout_candle") is True:
        add("Range Breakout", "BUY", ["Close is above the highest high of the previous 20 bars"])
    if patterns.get("breakdown_candle") is True:
        add("Range Breakdown", "SELL", ["Close is below the lowest low of the previous 20 bars"])
    if alignment == "bullish" and _state(components, "MACD")[0] == "bullish" and _state(components, "Supertrend")[0] == "bullish":
        add("Bullish Trend Continuation", "BUY", [alignment_detail, _state(components, "MACD")[1], _state(components, "Supertrend")[1]])
    if alignment == "bearish" and _state(components, "MACD")[0] == "bearish" and _state(components, "Supertrend")[0] == "bearish":
        add("Bearish Trend Continuation", "SELL", [alignment_detail, _state(components, "MACD")[1], _state(components, "Supertrend")[1]])
    prev_rsi, rsi = previous.get("rsi"), values.get("rsi")
    if prev_rsi is not None and rsi is not None and prev_rsi <= 30 and rsi > 30:
        add("RSI Reversal", "BUY", [f"RSI crossed up through 30, from {prev_rsi} to {rsi}"])
    if prev_rsi is not None and rsi is not None and prev_rsi >= 70 and rsi < 70:
        add("RSI Reversal", "SELL", [f"RSI crossed down through 70, from {prev_rsi} to {rsi}"])
    prev_macd, prev_signal = previous.get("macd"), previous.get("macd_signal")
    macd, macd_signal = values.get("macd"), values.get("macd_signal")
    if None not in (prev_macd, prev_signal, macd, macd_signal) and prev_macd <= prev_signal and macd > macd_signal:
        add("MACD Reversal", "BUY", ["MACD crossed above its signal on this bar"])
    if None not in (prev_macd, prev_signal, macd, macd_signal) and prev_macd >= prev_signal and macd < macd_signal:
        add("MACD Reversal", "SELL", ["MACD crossed below its signal on this bar"])
    if patterns.get("breakout_candle") is True and _state(components, "Relative volume")[0] == "bullish":
        add("Volume Breakout", "BUY", ["Close is above the prior 20-bar high", _state(components, "Relative volume")[1]])
    if patterns.get("breakdown_candle") is True and _state(components, "Relative volume")[0] == "bearish":
        add("Volume Breakdown", "SELL", ["Close is below the prior 20-bar low", _state(components, "Relative volume")[1]])
    if divergence["bullish"] is True:
        add("Bullish Divergence", "BUY", [divergence["detail"], "The newer swing low is lower in price and higher in RSI"])
    if divergence["bearish"] is True:
        add("Bearish Divergence", "SELL", [divergence["detail"], "The newer swing high is higher in price and lower in RSI"])
    return found


def confirmation_engine(signal, zones, mtf_rows):
    """Primary, confirmation, and invalidation lines that are true for this signal."""
    side = (signal or {}).get("signal")
    components = _component_map(signal)
    wanted = "bullish" if side == "BUY" else "bearish" if side == "SELL" else None
    primary = []
    confirmation = []
    if wanted:
        for name in ("Demand and supply", "Price action", "EMA alignment"):
            state, detail, points = _state(components, name)
            if state == wanted and points:
                primary.append(detail)
        for name in ("EMA 20 vs EMA 50", "RSI", "MACD", "Relative volume", "Supertrend", "ADX confirmation", "Session VWAP", "Price vs EMA 20"):
            state, detail, points = _state(components, name)
            if state == wanted and points:
                confirmation.append(detail)
        for row in mtf_rows or []:
            status = row.get("status")
            if side == "BUY" and status == "Bullish":
                confirmation.append(f"{row.get('label')} trend is bullish")
            elif side == "SELL" and status == "Bearish":
                confirmation.append(f"{row.get('label')} trend is bearish")
    demand = (zones or {}).get("demand") or {}
    supply = (zones or {}).get("supply") or {}
    stop = (signal or {}).get("stop_loss")
    if side == "BUY" and demand.get("bottom") is not None:
        invalidation = f"A close below the demand zone low at {demand['bottom']} invalidates the long."
    elif side == "BUY" and stop is not None:
        invalidation = f"A close below the stop at {stop} invalidates the long."
    elif side == "SELL" and supply.get("top") is not None:
        invalidation = f"A close above the supply zone high at {supply['top']} invalidates the short."
    elif side == "SELL" and stop is not None:
        invalidation = f"A close above the stop at {stop} invalidates the short."
    else:
        invalidation = "No invalidation level while the signal is Neutral."
    return {
        "signal": side,
        "primary": primary,
        "confirmation": confirmation,
        "invalidation": invalidation,
    }


def trade_plan(signal, zones):
    """Entry, stop, and three targets. Target 3 is 3R when that level is still free."""
    levels = (signal or {}).get("levels") or {}
    side = (signal or {}).get("signal")
    entry = levels.get("entry")
    stop = levels.get("stop_loss")
    target_1 = levels.get("target_1")
    target_2 = levels.get("target_2")
    target_3 = None
    target_3_basis = None
    if side in {"BUY", "SELL"} and entry is not None and stop is not None:
        risk = abs(float(entry) - float(stop))
        raw = float(entry) + (3 * risk if side == "BUY" else -3 * risk)
        used = [value for value in (target_1, target_2) if value is not None and abs(float(value) - raw) <= max(abs(float(entry)) * 0.001, 0.05)]
        if risk > 0 and not used:
            target_3 = _num(raw)
            target_3_basis = "3 times the stop distance"
    risk = None if entry is None or stop is None else _num(abs(float(entry) - float(stop)))
    rewards = {}
    for key, target in (("target_1", target_1), ("target_2", target_2), ("target_3", target_3)):
        rewards[key] = None if target is None or not risk else _num(abs(float(target) - float(entry)) / risk, 2)
    demand = (zones or {}).get("demand")
    supply = (zones or {}).get("supply")
    setup = levels.get("setup")
    if side == "BUY" and demand and "demand" in str(setup):
        entry_zone = f"Demand zone {_num(demand.get('bottom'))} to {_num(demand.get('top'))}. The quoted entry is still the close."
    elif side == "SELL" and supply and "supply" in str(setup):
        entry_zone = f"Supply zone {_num(supply.get('bottom'))} to {_num(supply.get('top'))}. The quoted entry is still the close."
    elif entry is not None:
        entry_zone = f"Entry is the {setup or 'current close'} at {entry}. This setup does not use a zone as the entry."
    else:
        entry_zone = "No entry while the signal is Neutral."
    return {
        "entry_zone": entry_zone,
        "entry": entry,
        "stop_loss": stop,
        "target_1": target_1,
        "target_2": target_2,
        "target_3": target_3,
        "target_3_basis": target_3_basis,
        "risk": risk,
        "reward_1": None if target_1 is None or entry is None else _num(abs(float(target_1) - float(entry))),
        "reward_multiples": rewards,
        "risk_reward": levels.get("risk_reward"),
        "formula": (levels.get("formula") or "") + (
            f" Target 3 uses {target_3_basis} at {target_3}." if target_3 is not None else " Target 3 is not added when that distance is already used or the signal is Neutral."
        ),
    }


def market_regime(frame):
    """Regime from ADX, ATR, and EMA structure on this frame only."""
    values = _snapshot_values(frame) if frame is not None and len(frame) else {}
    adx = values.get("adx")
    atr = values.get("atr")
    price = values.get("price")
    trend = trend_from_frame(frame) if frame is not None and len(frame) else {"status": "Unavailable", "detail": "No candles"}
    atr_ratio = None
    atr_basis = "ATR is missing"
    if frame is not None and "atr" in frame.columns and price:
        ratios = (frame["atr"] / frame["Close"]).replace([pd.NA, pd.NaT], pd.NA).dropna().tail(20)
        if len(ratios) >= 5 and atr is not None:
            typical = float(ratios.median())
            current = atr / price
            atr_ratio = _num(current / typical, 2) if typical else None
            atr_basis = f"Current ATR/price divided by the median of the last {len(ratios)} bars"
    conditions = [
        f"ADX {adx}" if adx is not None else "ADX is missing",
        trend.get("detail") or trend.get("status"),
        f"ATR {atr}" if atr is not None else "ATR is missing",
        f"ATR ratio {atr_ratio} ({atr_basis})" if atr_ratio is not None else atr_basis,
    ]
    if adx is None or trend.get("status") in {None, "Unavailable", "Insufficient data"}:
        label = "Unavailable"
    elif adx >= 25 and trend.get("status") == "Bullish":
        label = "Trending Bullish"
    elif adx >= 25 and trend.get("status") == "Bearish":
        label = "Trending Bearish"
    elif atr_ratio is not None and atr_ratio >= 1.5:
        label = "High Volatility"
    elif atr_ratio is not None and atr_ratio <= 0.7:
        label = "Low Volatility"
    elif adx < 20:
        label = "Range"
    else:
        label = "Range"
    return {"label": label, "conditions": [item for item in conditions if item], "atr_ratio": atr_ratio}


def signal_age(frame, include_vwap=False):
    """Bars since the indicator signal last changed.

    Zones are left out of every bar in this walk. Today's zone is not applied
    to an older bar.
    """
    if frame is None or len(frame) < 3:
        return {"bars": None, "status": "Data Unavailable"}
    current = build_signal_from_enriched(frame.iloc[max(0, len(frame) - 261):], include_vwap=include_vwap, zones_calculated=False)
    label = current.get("signal")
    if not label:
        return {"bars": None, "status": "Data Unavailable"}
    age = 1
    start = len(frame) - 2
    for offset in range(start, max(-1, start - 12), -1):
        window = frame.iloc[max(0, offset - 260): offset + 1]
        earlier = build_signal_from_enriched(window, include_vwap=include_vwap, zones_calculated=False)
        if earlier.get("signal") != label:
            return {
                "bars": age,
                "status": f"{age} bars",
                "capped": False,
                "basis": "Indicator rules only. Demand and supply are not part of the age count.",
            }
        age += 1
    return {
        "bars": age,
        "status": f"{age}+ bars",
        "capped": True,
        "basis": "Indicator rules only. Demand and supply are not part of the age count.",
    }


def position_size(capital, risk_pct, entry, stop):
    """Quantity from capital, risk percent, and the stop distance. No order is sent."""
    try:
        capital = float(capital)
        risk_pct = float(risk_pct)
        entry = float(entry)
        stop = float(stop)
    except (TypeError, ValueError):
        return {"data_unavailable": True, "message": "Capital, risk, entry, and stop must be numbers"}
    if capital <= 0 or risk_pct <= 0 or risk_pct > 100 or entry == stop:
        return {"data_unavailable": True, "message": "Capital and risk must be positive, and the stop must differ from the entry"}
    risk_amount = capital * risk_pct / 100.0
    quantity = risk_amount / abs(entry - stop)
    return {
        "risk_amount": _num(risk_amount),
        "quantity": _num(quantity, 4),
        "maximum_loss": _num(risk_amount),
        "formula": "Risk amount = capital × risk percent. Quantity = risk amount / absolute stop distance. No order is placed.",
    }


def assemble_report(frame, signal, zones, mtf_rows, include_vwap=False, zones_calculated=False):
    """One intelligence payload for the current decision bar."""
    if not signal or signal.get("data_unavailable"):
        return {"data_unavailable": True, "message": "Data Unavailable"}
    plan = trade_plan(signal, zones)
    return {
        "setups": detect_setups(frame, signal, zones),
        "confirmation": confirmation_engine(signal, zones, mtf_rows),
        "quality": quality_scores(signal, mtf_rows),
        "trade_plan": plan,
        "regime": market_regime(frame),
        "signal_age": signal_age(frame, include_vwap=include_vwap),
        "omitted_patterns": [
            "Cup and handle, double bottom, breakout retest, and Ichimoku are not calculated, so they are not listed.",
        ],
    }


def _window(frame, start, end, limit):
    prepared = prepare_ohlcv(frame)
    if prepared.empty or len(prepared) < 40:
        return None, {"data_unavailable": True, "message": "DATA UNAVAILABLE"}
    index = prepared.index
    if getattr(index, "tz", None) is not None:
        prepared = prepared.copy()
        prepared.index = index.tz_convert("Asia/Kolkata").tz_localize(None)
    start_ts = pd.Timestamp(start).tz_localize(None) if start and pd.Timestamp(start).tzinfo else (pd.Timestamp(start) if start else None)
    end_ts = pd.Timestamp(end).tz_localize(None) if end and pd.Timestamp(end).tzinfo else (pd.Timestamp(end) if end else None)
    last_pos = len(prepared) - 1
    if end_ts is not None:
        bounded = [pos for pos, stamp in enumerate(prepared.index) if stamp <= end_ts]
        if not bounded:
            return None, {"data_unavailable": True, "message": "DATA UNAVAILABLE"}
        last_pos = bounded[-1]
    prepared = prepared.iloc[: last_pos + 1]
    first = 35
    if start_ts is not None:
        visible = [pos for pos, stamp in enumerate(prepared.index) if stamp >= start_ts]
        if not visible:
            return None, {"data_unavailable": True, "message": "DATA UNAVAILABLE"}
        first = max(35, visible[0])
    if last_pos - first < 1:
        return None, {"data_unavailable": True, "message": "DATA UNAVAILABLE"}
    capped = False
    if last_pos - first + 1 > limit:
        first = last_pos - limit + 1
        capped = True
    return (prepared, first, last_pos, capped), None


def _candle_row(frame, pos):
    row = frame.iloc[pos]
    return {
        "time": int(pd.Timestamp(frame.index[pos]).timestamp()),
        "open": _num(row["Open"]),
        "high": _num(row["High"]),
        "low": _num(row["Low"]),
        "close": _num(row["Close"]),
        "volume": _num(row["Volume"], 0),
    }


def build_replay(frame, timeframe, start=None, end=None, limit=80):
    """Signals for each replay bar use only candles up to that bar. Zones are excluded."""
    window, error = _window(frame, start, end, limit)
    if error:
        return error
    prepared, first, last_pos, capped = window
    enriched = add_indicators(prepared, include_vwap=False)
    context_from = max(0, first - 40)
    candles = [_candle_row(prepared, pos) for pos in range(context_from, last_pos + 1)]
    steps = []
    for pos in range(first, last_pos + 1):
        signal = build_signal_from_enriched(
            enriched.iloc[max(0, pos - 260): pos + 1],
            include_vwap=False,
            zones_calculated=False,
        )
        steps.append({
            "time": int(pd.Timestamp(prepared.index[pos]).timestamp()),
            "signal": signal.get("signal"),
            "label": signal.get("label"),
            "strength": signal.get("strength"),
            "entry": signal.get("entry"),
            "stop_loss": signal.get("stop_loss"),
            "target_1": signal.get("target_1"),
            "target_2": signal.get("target_2"),
            "reasons": signal.get("reasons") or [],
            "components": [
                {"name": row.get("name"), "points": row.get("points"), "detail": row.get("detail"), "state": row.get("state")}
                for row in signal.get("components") or []
            ],
        })
    return {
        "timeframe": timeframe,
        "candles": candles,
        "steps": steps,
        "capped": capped,
        "zones": "Data Unavailable",
        "methodology": (
            "Each step is scored from candles up to and including that bar. "
            "Later candles in this response are for playback only and are not an input to an earlier step. "
            "Demand and supply are excluded on replay bars, not copied from a later date. "
            + ("The window was limited to the most recent bars inside the selected dates." if capped else "")
        ),
    }


def _gate(preset, signal, weekly_status):
    components = _component_map(signal)
    side = signal.get("signal")
    wanted = "bullish" if side == "BUY" else "bearish" if side == "SELL" else None
    if wanted is None:
        return False
    if preset == "ema_rsi":
        return _state(components, "EMA alignment")[0] == wanted and _state(components, "RSI")[0] == wanted
    if preset == "breakout_volume":
        detail = _state(components, "Price action")[1] or ""
        token = "breakout_candle" if side == "BUY" else "breakdown_candle"
        return token in detail and _state(components, "Relative volume")[0] == wanted
    if preset == "multi_timeframe":
        weekly = "Bullish" if side == "BUY" else "Bearish"
        return _state(components, "EMA alignment")[0] == wanted and weekly_status == weekly
    return False


def _weekly_status(prefix):
    weekly = resample_chart(prefix, "1wk")
    if weekly is None or len(prepare_ohlcv(weekly)) < 20:
        return "Unavailable"
    return trend_from_frame(weekly).get("status")


def _book_metrics(name, trades, capital, cash, max_drawdown):
    wins = [trade for trade in trades if (trade["pnl"] or 0) > 0]
    losses = [trade for trade in trades if (trade["pnl"] or 0) < 0]
    gross_profit = sum(trade["pnl"] for trade in wins)
    gross_loss = abs(sum(trade["pnl"] for trade in losses))
    avg_win = None if not wins else _num(gross_profit / len(wins))
    avg_loss = None if not losses else _num(-gross_loss / len(losses))
    expectancy = None
    if trades and avg_win is not None and avg_loss is not None:
        expectancy = _num((len(wins) / len(trades)) * avg_win + (len(losses) / len(trades)) * avg_loss)
    elif trades and avg_win is not None:
        expectancy = avg_win
    elif trades and avg_loss is not None:
        expectancy = avg_loss
    return {
        "name": name,
        "total_trades": len(trades),
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "win_rate": None if not trades else _num(100 * len(wins) / len(trades), 2),
        "profit_factor": None if gross_loss == 0 else _num(gross_profit / gross_loss),
        "net_profit": _num(cash - float(capital)),
        "max_drawdown_pct": _num(max_drawdown),
        "expectancy": expectancy,
        "average_win": avg_win,
        "average_loss": avg_loss,
    }


def compare_presets(frame, timeframe, capital=100000.0, risk_pct=1.0, start=None, end=None, limit=120):
    """One causal pass, four separate books. Books are not ranked."""
    window, error = _window(frame, start, end, limit)
    if error:
        return error
    prepared, first, last_pos, capped = window
    enriched = add_indicators(prepared, include_vwap=False)
    names = ("ema_rsi", "breakout_volume", "multi_timeframe")
    books = {
        name: {"cash": float(capital), "peak": float(capital), "drawdown": 0.0, "position": None, "trades": []}
        for name in names
    }
    for pos in range(first, last_pos + 1):
        prefix = enriched.iloc[: pos + 1]
        signal = build_signal_from_enriched(prefix.iloc[max(0, len(prefix) - 260):], include_vwap=False, zones_calculated=False)
        weekly = _weekly_status(prefix.tail(400))
        high = float(prepared["High"].iloc[pos])
        low = float(prepared["Low"].iloc[pos])
        for name in names:
            book = books[name]
            position = book["position"]
            if position is not None and pos >= position["entry_index"]:
                hit_stop = low <= position["stop"] if position["side"] == "buy" else high >= position["stop"]
                hit_target = position["target"] is not None and (high >= position["target"] if position["side"] == "buy" else low <= position["target"])
                exit_price = None
                reason = None
                if hit_stop:
                    exit_price, reason = position["stop"], "stop loss (same bar as target; stop is assumed first)" if hit_target else "stop loss"
                elif hit_target:
                    exit_price, reason = position["target"], "target 1"
                elif signal.get("signal") in {"BUY", "SELL"} and ((position["side"] == "buy" and signal["signal"] == "SELL") or (position["side"] == "sell" and signal["signal"] == "BUY")) and pos + 1 <= last_pos:
                    exit_price, reason = float(prepared["Open"].iloc[pos + 1]), "opposite signal"
                elif pos == last_pos:
                    exit_price, reason = float(prepared["Close"].iloc[pos]), "end of test"
                if exit_price is not None:
                    direction = 1 if position["side"] == "buy" else -1
                    pnl = position["quantity"] * (exit_price - position["entry"]) * direction
                    book["cash"] += pnl
                    book["peak"] = max(book["peak"], book["cash"])
                    if book["peak"]:
                        book["drawdown"] = max(book["drawdown"], (book["peak"] - book["cash"]) / book["peak"] * 100)
                    book["trades"].append({"pnl": _num(pnl), "side": position["side"], "reason": reason})
                    book["position"] = None
            if book["position"] is None and pos < last_pos and _gate(name, signal, weekly) and signal.get("stop_loss") is not None:
                entry = float(prepared["Open"].iloc[pos + 1])
                stop = float(signal["stop_loss"])
                side = "buy" if signal["signal"] == "BUY" else "sell"
                if (side == "buy" and entry <= stop) or (side == "sell" and entry >= stop):
                    continue
                risk = abs(entry - stop)
                risk_cash = book["cash"] * (float(risk_pct) / 100.0)
                if risk <= 0 or risk_cash <= 0 or book["cash"] <= 0:
                    continue
                book["position"] = {
                    "side": side,
                    "entry_index": pos + 1,
                    "entry": entry,
                    "stop": stop,
                    "target": None if signal.get("target_1") is None else float(signal["target_1"]),
                    "quantity": risk_cash / risk,
                }
    descriptions = {
        "ema_rsi": "Entry only when the signal is directional and both EMA alignment and RSI agree with it.",
        "breakout_volume": "Entry only when the signal is directional, the breakout or breakdown candle is the price-action reason, and relative volume agrees.",
        "multi_timeframe": "Entry only when EMA alignment agrees with the signal and the weekly trend, from candles up to that bar, agrees too.",
        "demand_ema": "Not run. Demand and supply are not calculated on every comparison bar, so this book is unavailable instead of using a later zone.",
    }
    rows = [_book_metrics(name, books[name]["trades"], capital, books[name]["cash"], books[name]["drawdown"]) | {"rule": descriptions[name]} for name in names]
    rows.append({
        "name": "demand_ema",
        "data_unavailable": True,
        "message": descriptions["demand_ema"],
        "rule": descriptions["demand_ema"],
        "total_trades": None,
        "win_rate": None,
        "profit_factor": None,
        "net_profit": None,
        "max_drawdown_pct": None,
        "expectancy": None,
    })
    return {
        "disclaimer": "These figures describe this historical test only. They are not a guarantee of future performance. The books are separate. They are not ranked.",
        "methodology": "One pass scores each bar from candles up to that bar. Each book enters and exits on its own. Demand and supply are not part of this comparison.",
        "capped": capped,
        "timeframe": timeframe,
        "strategies": rows,
    }
