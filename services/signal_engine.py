"""Rule-based BUY/SELL signals and causal backtests.

Every component is worth +10 or -10 only when that calculation exists.
Missing data is excluded. It is never turned into a reason.

A backtest bar may use only candles up to that bar. The fill is the next
bar's open. These results describe the test window only.
"""

import pandas as pd

from services import sd_zones
from services.technical_analysis import (
    _fib_levels,
    _last_patterns,
    _num,
    _range_text,
    _snapshot_values,
    _zone_nearby,
    add_indicators,
    ema_alignment,
    nearest_zones,
    prepare_ohlcv,
    trend_from_frame,
)

POINT = 10
BULLISH_PATTERNS = (
    "bullish_engulfing", "hammer", "inverted_hammer", "morning_star", "piercing", "breakout_candle",
)
BEARISH_PATTERNS = (
    "bearish_engulfing", "shooting_star", "hanging_man", "evening_star", "dark_cloud", "breakdown_candle",
)
LABEL_RULES = (
    {"label": "Strong Buy", "signal": "BUY", "rule": "Bullish ratio >= 0.60"},
    {"label": "Buy", "signal": "BUY", "rule": "Bullish ratio >= 0.25 and < 0.60"},
    {"label": "Neutral", "signal": "NEUTRAL", "rule": "Ratio between -0.25 and 0.25"},
    {"label": "Sell", "signal": "SELL", "rule": "Bearish ratio <= -0.25 and > -0.60"},
    {"label": "Strong Sell", "signal": "SELL", "rule": "Bearish ratio <= -0.60"},
)


def _component(name, points, detail, state):
    return {"name": name, "points": points, "detail": detail, "state": state}


def _label_for(ratio):
    if ratio >= 0.60:
        return "Strong Buy", "BUY"
    if ratio >= 0.25:
        return "Buy", "BUY"
    if ratio <= -0.60:
        return "Strong Sell", "SELL"
    if ratio <= -0.25:
        return "Sell", "SELL"
    return "Neutral", "NEUTRAL"


def _pattern_component(patterns):
    known = [patterns.get(name) for name in BULLISH_PATTERNS + BEARISH_PATTERNS]
    if not known or all(item is None for item in known):
        return _component("Price action", None, "Not enough candles to judge the listed patterns", "unavailable")
    bullish = [name for name in BULLISH_PATTERNS if patterns.get(name) is True]
    bearish = [name for name in BEARISH_PATTERNS if patterns.get(name) is True]
    if bullish and not bearish:
        return _component("Price action", POINT, "Bullish pattern: " + ", ".join(bullish), "bullish")
    if bearish and not bullish:
        return _component("Price action", -POINT, "Bearish pattern: " + ", ".join(bearish), "bearish")
    if bullish and bearish:
        return _component("Price action", 0, "Bullish and bearish patterns both matched", "neutral")
    return _component("Price action", 0, "None of the directional patterns matched", "neutral")


def score_components(values, patterns, zones, zones_calculated, vwap_enabled):
    """One row per rule. Points are +10, -10, 0, or null when the input is missing."""
    rows = []
    alignment = ema_alignment(values)
    if alignment == "Unavailable":
        rows.append(_component("EMA alignment", None, "EMA 20, 50, or 200 is missing", "unavailable"))
    elif alignment == "Bullish":
        rows.append(_component("EMA alignment", POINT, "EMA 20 > EMA 50 > EMA 200", "bullish"))
    elif alignment == "Bearish":
        rows.append(_component("EMA alignment", -POINT, "EMA 20 < EMA 50 < EMA 200", "bearish"))
    else:
        rows.append(_component("EMA alignment", 0, "EMA order is mixed", "neutral"))

    price, ema20 = values.get("price"), values.get("ema20")
    if price is None or ema20 is None:
        rows.append(_component("Price vs EMA 20", None, "Price or EMA 20 is missing", "unavailable"))
    elif price > ema20:
        rows.append(_component("Price vs EMA 20", POINT, "Close is above EMA 20", "bullish"))
    elif price < ema20:
        rows.append(_component("Price vs EMA 20", -POINT, "Close is below EMA 20", "bearish"))
    else:
        rows.append(_component("Price vs EMA 20", 0, "Close equals EMA 20", "neutral"))

    ema50 = values.get("ema50")
    if ema20 is None or ema50 is None:
        rows.append(_component("EMA 20 vs EMA 50", None, "EMA 20 or EMA 50 is missing", "unavailable"))
    elif ema20 > ema50:
        rows.append(_component("EMA 20 vs EMA 50", POINT, "EMA 20 is above EMA 50", "bullish"))
    elif ema20 < ema50:
        rows.append(_component("EMA 20 vs EMA 50", -POINT, "EMA 20 is below EMA 50", "bearish"))
    else:
        rows.append(_component("EMA 20 vs EMA 50", 0, "EMA 20 equals EMA 50", "neutral"))

    rsi = values.get("rsi")
    if rsi is None:
        rows.append(_component("RSI", None, "RSI is missing", "unavailable"))
    elif rsi >= 55:
        rows.append(_component("RSI", POINT, f"RSI {rsi} is at least 55", "bullish"))
    elif rsi <= 45:
        rows.append(_component("RSI", -POINT, f"RSI {rsi} is at most 45", "bearish"))
    else:
        rows.append(_component("RSI", 0, f"RSI {rsi} is between 45 and 55", "neutral"))

    macd, signal = values.get("macd"), values.get("macd_signal")
    if macd is None or signal is None:
        rows.append(_component("MACD", None, "MACD or its signal is missing", "unavailable"))
    elif macd > signal:
        rows.append(_component("MACD", POINT, "MACD is above its signal", "bullish"))
    elif macd < signal:
        rows.append(_component("MACD", -POINT, "MACD is below its signal", "bearish"))
    else:
        rows.append(_component("MACD", 0, "MACD equals its signal", "neutral"))

    adx = values.get("adx")
    if adx is None or alignment == "Unavailable":
        rows.append(_component("ADX confirmation", None, "ADX or EMA trend is missing", "unavailable"))
    elif adx > 25 and alignment == "Bullish":
        rows.append(_component("ADX confirmation", POINT, f"ADX {adx} confirms the bullish EMA trend", "bullish"))
    elif adx > 25 and alignment == "Bearish":
        rows.append(_component("ADX confirmation", -POINT, f"ADX {adx} confirms the bearish EMA trend", "bearish"))
    else:
        rows.append(_component("ADX confirmation", 0, f"ADX {adx} does not confirm a stacked EMA trend", "neutral"))

    direction = values.get("supertrend_dir")
    if direction is None:
        rows.append(_component("Supertrend", None, "Supertrend is missing", "unavailable"))
    elif direction > 0:
        rows.append(_component("Supertrend", POINT, "Supertrend is bullish", "bullish"))
    elif direction < 0:
        rows.append(_component("Supertrend", -POINT, "Supertrend is bearish", "bearish"))
    else:
        rows.append(_component("Supertrend", 0, "Supertrend has no direction", "neutral"))

    vwap = values.get("vwap")
    if not vwap_enabled or vwap is None or price is None:
        rows.append(_component("Session VWAP", None, "Session VWAP is not calculated on this timeframe", "unavailable"))
    elif price > vwap:
        rows.append(_component("Session VWAP", POINT, "Close is above session VWAP", "bullish"))
    elif price < vwap:
        rows.append(_component("Session VWAP", -POINT, "Close is below session VWAP", "bearish"))
    else:
        rows.append(_component("Session VWAP", 0, "Close equals session VWAP", "neutral"))

    rvol = values.get("rvol")
    if rvol is None or price is None or values.get("change_pct") is None:
        rows.append(_component("Relative volume", None, "Relative volume or the last bar change is missing", "unavailable"))
    elif rvol > 1.5 and values["change_pct"] > 0:
        rows.append(_component("Relative volume", POINT, f"Relative volume {rvol} confirms an up bar", "bullish"))
    elif rvol > 1.5 and values["change_pct"] < 0:
        rows.append(_component("Relative volume", -POINT, f"Relative volume {rvol} confirms a down bar", "bearish"))
    else:
        rows.append(_component("Relative volume", 0, f"Relative volume {rvol} is not above 1.5 in the bar's direction", "neutral"))

    if not zones_calculated:
        rows.append(_component("Demand and supply", None, "Zones were not calculated for this bar", "unavailable"))
    else:
        demand = (zones or {}).get("demand")
        supply = (zones or {}).get("supply")
        near_demand = _zone_nearby(demand, price, "demand")
        near_supply = _zone_nearby(supply, price, "supply")
        if near_demand and not near_supply:
            rows.append(_component("Demand and supply", POINT, "Price is inside or within 5% of demand", "bullish"))
        elif near_supply and not near_demand:
            rows.append(_component("Demand and supply", -POINT, "Price is inside or within 5% of supply", "bearish"))
        elif near_demand and near_supply:
            rows.append(_component("Demand and supply", 0, "Price is near both demand and supply", "neutral"))
        else:
            rows.append(_component("Demand and supply", 0, "No calculated zone is near the price", "neutral"))

    rows.append(_pattern_component(patterns or {}))
    return rows


def _pick_stop(entry, options, atr, side):
    """Nearest structural stop that is not inside a fraction of ATR."""
    valid = [(name, level) for name, level in options if level is not None and ((side == "buy" and level < entry) or (side == "sell" and level > entry))]
    if not valid:
        return None, None
    valid.sort(key=lambda item: item[1], reverse=(side == "buy"))
    cushion = (atr or 0) * 0.15
    for name, level in valid:
        distance = abs(entry - level)
        if cushion and distance < cushion and len(valid) > 1:
            continue
        return name, level
    return valid[0]


def _targets(entry, side, supply, demand, fib_prices, risk):
    levels = []
    if side == "buy":
        if supply and supply.get("bottom") is not None and float(supply["bottom"]) > entry:
            levels.append(("next supply zone", float(supply["bottom"])))
        for price in sorted(price for price in fib_prices if price > entry):
            levels.append(("Fibonacci level", price))
        if risk and risk > 0:
            levels.append(("2 times the stop distance", entry + 2 * risk))
            levels.append(("3 times the stop distance", entry + 3 * risk))
        levels.sort(key=lambda item: item[1])
    else:
        if demand and demand.get("top") is not None and float(demand["top"]) < entry:
            levels.append(("next demand zone", float(demand["top"])))
        for price in sorted((price for price in fib_prices if price < entry), reverse=True):
            levels.append(("Fibonacci level", price))
        if risk and risk > 0:
            levels.append(("2 times the stop distance", entry - 2 * risk))
            levels.append(("3 times the stop distance", entry - 3 * risk))
        levels.sort(key=lambda item: item[1], reverse=True)
    chosen = []
    for name, level in levels:
        if chosen and abs(level - chosen[-1][1]) <= max(abs(entry) * 0.001, 0.01):
            continue
        chosen.append((name, level))
        if len(chosen) == 2:
            break
    first = chosen[0] if chosen else (None, None)
    second = chosen[1] if len(chosen) > 1 else (None, None)
    return first, second


def trade_levels(side, values, zones, frame):
    price = values.get("price")
    if side not in {"buy", "sell"} or price is None:
        return {
            "entry": None, "stop_loss": None, "target_1": None, "target_2": None,
            "risk_reward": None, "setup": None,
            "formula": "No trade levels while the signal is Neutral.",
        }
    atr = values.get("atr")
    demand = (zones or {}).get("demand")
    supply = (zones or {}).get("supply")
    prior = frame.iloc[:-1]
    swing = prior.tail(20) if len(prior) else prior
    swing_low = None if swing.empty else _num(swing["Low"].min(), 4)
    swing_high = None if swing.empty else _num(swing["High"].max(), 4)
    patterns, _fib = _last_patterns(frame, price)
    if patterns.get("breakout_candle") is True and side == "buy":
        setup = "breakout close"
    elif patterns.get("breakdown_candle") is True and side == "sell":
        setup = "breakdown close"
    elif side == "buy" and _zone_nearby(demand, price, "demand"):
        setup = "demand-zone confirmation at the current close"
    elif side == "sell" and _zone_nearby(supply, price, "supply"):
        setup = "supply-zone confirmation at the current close"
    else:
        setup = "current close"
    entry = float(price)
    buffer = (atr or 0) * 0.5
    if side == "buy":
        options = []
        if demand and demand.get("bottom") is not None:
            options.append(("demand zone low minus 0.5 ATR", float(demand["bottom"]) - buffer))
        if swing_low is not None:
            options.append(("lowest low of the previous 20 bars", float(swing_low)))
        if atr:
            options.append(("one ATR below entry", entry - float(atr)))
    else:
        options = []
        if supply and supply.get("top") is not None:
            options.append(("supply zone high plus 0.5 ATR", float(supply["top"]) + buffer))
        if swing_high is not None:
            options.append(("highest high of the previous 20 bars", float(swing_high)))
        if atr:
            options.append(("one ATR above entry", entry + float(atr)))
    stop_name, stop = _pick_stop(entry, options, atr, side)
    if stop is None:
        return {
            "entry": _num(entry), "stop_loss": None, "target_1": None, "target_2": None,
            "risk_reward": None, "setup": setup,
            "formula": f"Entry is the {setup}. No structural stop is available below the entry." if side == "buy" else f"Entry is the {setup}. No structural stop is available above the entry.",
        }
    risk = abs(entry - stop)
    fib_prices = [price for price in _fib_levels(frame).values() if price is not None]
    (target_name, target_1), (target_2_name, target_2) = _targets(entry, side, supply, demand, fib_prices, risk)
    reward = None if target_1 is None else abs(target_1 - entry)
    ratio = None if not risk or reward is None else _num(reward / risk, 2)
    formula = (
        f"Entry is the {setup} at {entry:.2f}. "
        f"Stop uses {stop_name} at {stop:.2f}. "
        f"Target 1 uses {target_name or 'no level'} at {None if target_1 is None else round(target_1, 2)}. "
        f"Target 2 uses {target_2_name or 'no further level'} at {None if target_2 is None else round(target_2, 2)}. "
        f"Risk/reward uses target 1 distance divided by stop distance"
        + (f" = 1:{ratio}." if ratio is not None else ".")
    )
    return {
        "entry": _num(entry),
        "stop_loss": _num(stop),
        "target_1": _num(target_1),
        "target_2": _num(target_2),
        "risk_reward": None if ratio is None else f"1:{ratio}",
        "risk_reward_value": ratio,
        "setup": setup,
        "stop_basis": stop_name,
        "target_1_basis": target_name,
        "target_2_basis": target_2_name,
        "formula": formula,
    }


def build_signal_from_enriched(enriched, include_vwap=False, zones=None, zones_calculated=False):
    """Score an indicator frame that already ends on the decision bar."""
    if enriched is None or enriched.empty or _num(enriched["Close"].iloc[-1]) is None:
        return {"data_unavailable": True, "message": "DATA UNAVAILABLE"}
    if zones_calculated and zones is None:
        zones = nearest_zones(enriched, ("1d",)).get("1d")
    values = _snapshot_values(enriched)
    if include_vwap and values.get("vwap") is not None:
        values["vwap_basis"] = "session"
    patterns, _fib = _last_patterns(enriched, values.get("price"))
    if not zones_calculated:
        zones = {"demand": None, "supply": None}
    components = score_components(values, patterns, zones, zones_calculated, include_vwap)
    evaluated = [row for row in components if row["points"] is not None]
    net = sum(row["points"] for row in evaluated)
    maximum = POINT * len(evaluated)
    ratio = 0 if maximum == 0 else net / maximum
    label, signal = _label_for(ratio)
    strength = 0 if maximum == 0 else int(round(abs(net) / maximum * 100))
    side = "buy" if signal == "BUY" else "sell" if signal == "SELL" else None
    levels = trade_levels(side, values, zones if zones_calculated else {}, enriched)
    supportive = [row for row in components if row["state"] == ("bullish" if signal == "BUY" else "bearish" if signal == "SELL" else "")]
    trend = trend_from_frame(enriched)
    demand = None if not zones else zones.get("demand")
    supply = None if not zones else zones.get("supply")
    return {
        "price": values.get("price"),
        "signal": signal,
        "label": label,
        "strength": strength,
        "net_points": net,
        "maximum_points": maximum,
        "ratio": _num(ratio, 4),
        "point_value": POINT,
        "label_rules": list(LABEL_RULES),
        "score_formula": (
            f"Each available rule is +{POINT} or -{POINT}. "
            f"Net {net} / maximum {maximum} = ratio {ratio:.2f}. "
            f"Strength is the absolute ratio as a percentage. "
            "Unavailable rules are excluded."
        ),
        "components": components,
        "reasons": [row["detail"] for row in supportive],
        "entry": levels.get("entry"),
        "stop_loss": levels.get("stop_loss"),
        "target_1": levels.get("target_1"),
        "target_2": levels.get("target_2"),
        "risk_reward": levels.get("risk_reward"),
        "levels": levels,
        "trend": trend.get("status"),
        "demand": _range_text(demand),
        "supply": _range_text(supply),
        "time": int(pd.Timestamp(enriched.index[-1]).timestamp()) if len(enriched.index) else None,
    }


def build_signal(frame, include_vwap=False, zones=None, zones_calculated=False):
    """Score the last bar of frame. The frame must already end at the decision bar."""
    prepared = prepare_ohlcv(frame)
    enriched = add_indicators(prepared, include_vwap=include_vwap)
    return build_signal_from_enriched(enriched, include_vwap, zones, zones_calculated)


def _bar_time(value):
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is not None:
        stamp = stamp.tz_convert("Asia/Kolkata").tz_localize(None)
    return int(stamp.timestamp())


def _hit(side, high, low, stop, target):
    if side == "buy":
        stopped = low <= stop
        targeted = target is not None and high >= target
    else:
        stopped = high >= stop
        targeted = target is not None and low <= target
    if stopped and targeted:
        return "stop", "stop loss (same bar as target; stop is assumed first)"
    if stopped:
        return "stop", "stop loss"
    if targeted:
        return "target", "target 1"
    return None, None


def _trade_zone(prefix, timeframe, side, price, zone_pair=None):
    """Demand behind a BUY or supply behind a SELL, from candles up to the signal bar."""
    kind = "demand" if side == "buy" else "supply"
    if zone_pair is None:
        try:
            if timeframe == "1d":
                zone_pair = nearest_zones(prefix, ("1d",)).get("1d") or {}
            else:
                zone_pair = sd_zones.frame_nearest(prefix.tail(400), timeframe)
        except Exception:
            zone_pair = {}
    zone = (zone_pair or {}).get(kind)
    if not zone:
        return None
    return {
        "type": kind,
        "top": zone.get("top"),
        "bottom": zone.get("bottom"),
        "time": zone.get("time"),
        "label": zone.get("label") or f"{kind.title()} zone",
        "strength": zone.get("strength"),
        "freshness": zone.get("freshness") or zone.get("status"),
        "distance_pct": sd_zones.distance_pct(zone, price) if price else None,
        "nearby": bool(_zone_nearby(zone, price, kind)),
    }


def run_backtest(frame, timeframe, strategy="indicator", capital=100000.0, risk_pct=1.0, start=None, end=None, progress=None):
    """Walk candles in order. Bar N never sees bar N+1 except as the next open fill."""
    prepared = prepare_ohlcv(frame)
    if strategy not in {"indicator", "structure"}:
        raise ValueError("Strategy must be indicator or structure")
    if prepared.empty or len(prepared) < 40:
        return {"data_unavailable": True, "message": "DATA UNAVAILABLE"}
    include_vwap = False
    if isinstance(prepared.index, pd.DatetimeIndex):
        counts = prepared.index.normalize().value_counts()
        include_vwap = int(counts.max()) >= 2 if len(counts) else False
    start_ts = pd.Timestamp(start) if start else None
    end_ts = pd.Timestamp(end) if end else None
    if start_ts is not None and start_ts.tzinfo is not None:
        start_ts = start_ts.tz_localize(None)
    if end_ts is not None and end_ts.tzinfo is not None:
        end_ts = end_ts.tz_localize(None)
    index = prepared.index
    if getattr(index, "tz", None) is not None:
        index = index.tz_convert("Asia/Kolkata").tz_localize(None)
        prepared = prepared.copy()
        prepared.index = index
    first = 35
    if start_ts is not None:
        visible = [pos for pos, stamp in enumerate(prepared.index) if stamp >= start_ts]
        if not visible:
            return {"data_unavailable": True, "message": "DATA UNAVAILABLE"}
        first = max(35, visible[0])
    last_pos = len(prepared) - 1
    if end_ts is not None:
        bounded = [pos for pos, stamp in enumerate(prepared.index) if stamp <= end_ts]
        if not bounded:
            return {"data_unavailable": True, "message": "DATA UNAVAILABLE"}
        last_pos = bounded[-1]
    if last_pos - first < 2:
        return {"data_unavailable": True, "message": "DATA UNAVAILABLE"}
    prepared = prepared.iloc[: last_pos + 1]
    last_pos = len(prepared) - 1

    enriched = add_indicators(prepared, include_vwap=include_vwap)
    trades = []
    equity = [{"time": _bar_time(prepared.index[first]), "equity": _num(capital, 2)}]
    cash = float(capital)
    peak = cash
    max_drawdown = 0.0
    position = None
    zone_cache = None
    include_zones = strategy == "structure"

    def close_trade(exit_index, exit_price, reason, kind):
        nonlocal cash, peak, max_drawdown, position
        direction = 1 if position["side"] == "buy" else -1
        pnl = position["quantity"] * (exit_price - position["entry"]) * direction
        cash += pnl
        peak = max(peak, cash)
        if peak:
            max_drawdown = max(max_drawdown, (peak - cash) / peak * 100)
        held = max(1, exit_index - position["entry_index"] + (0 if kind == "signal" else 1))
        change_pct = (exit_price - position["entry"]) / position["entry"] * 100 * direction
        trades.append({
            "trade": len(trades) + 1,
            "time": _bar_time(prepared.index[position["entry_index"]]),
            "exit_time": _bar_time(prepared.index[exit_index]),
            "symbol": None,
            "timeframe": timeframe,
            "side": "BUY" if position["side"] == "buy" else "SELL",
            "label": position["label"],
            "entry": _num(position["entry"]),
            "stop_loss": _num(position["stop"]),
            "target_1": _num(position["target"]),
            "target_2": _num(position["target_2"]),
            "exit": _num(exit_price),
            "exit_reason": reason,
            "pnl": _num(pnl),
            "pnl_pct": _num(change_pct),
            "holding_bars": int(held),
            "score": position["score"],
            "reasons": position["reasons"],
            "mfe": _num(max(position["mfe"], 0)),
            "mae": _num(max(position["mae"], 0)),
            "zone": position["zone"],
            "zone_used": include_zones,
        })
        equity.append({"time": trades[-1]["exit_time"], "equity": _num(cash, 2)})
        position = None

    for i in range(first, last_pos + 1):
        if progress and i % 25 == 0:
            progress(f"Bar {i - first + 1} / {last_pos - first + 1}")
        window = enriched.iloc[max(0, i - 260): i + 1]
        if position is not None and i >= position["entry_index"]:
            high = float(prepared["High"].iloc[i])
            low = float(prepared["Low"].iloc[i])
            if position["side"] == "buy":
                position["mfe"] = max(position["mfe"], high - position["entry"])
                position["mae"] = max(position["mae"], position["entry"] - low)
            else:
                position["mfe"] = max(position["mfe"], position["entry"] - low)
                position["mae"] = max(position["mae"], high - position["entry"])
            kind, reason = _hit(position["side"], high, low, position["stop"], position["target"])
            if kind == "stop":
                close_trade(i, position["stop"], reason, kind)
            elif kind == "target":
                close_trade(i, position["target"], reason, kind)
            elif i == last_pos:
                close_trade(i, float(prepared["Close"].iloc[i]), "end of test", "end")
        if position is not None or i >= last_pos:
            if position is not None and i < last_pos:
                cached_zones = None if not zone_cache else zone_cache["zones"]
                signal = build_signal_from_enriched(
                    window,
                    include_vwap=include_vwap,
                    zones=cached_zones,
                    zones_calculated=cached_zones is not None,
                )
                opposite = (
                    (position["side"] == "buy" and signal.get("signal") == "SELL")
                    or (position["side"] == "sell" and signal.get("signal") == "BUY")
                )
                if opposite and i + 1 <= last_pos:
                    close_trade(i + 1, float(prepared["Open"].iloc[i + 1]), "opposite signal", "signal")
            continue
        zones = None
        zones_calculated = False
        if include_zones and zone_cache and i - zone_cache["index"] < 5:
            zones = zone_cache["zones"]
            zones_calculated = True
        signal = build_signal_from_enriched(window, include_vwap=include_vwap, zones=zones, zones_calculated=zones_calculated)
        if signal.get("data_unavailable"):
            continue
        if include_zones and signal["signal"] in {"BUY", "SELL"} and not zones_calculated:
            zone_pair = nearest_zones(enriched.iloc[: i + 1], ("1d",)).get("1d")
            zone_cache = {"index": i, "zones": zone_pair}
            signal = build_signal_from_enriched(window, include_vwap=include_vwap, zones=zone_pair, zones_calculated=True)
        if signal["signal"] not in {"BUY", "SELL"} or signal.get("stop_loss") is None:
            continue
        entry_bar = i + 1
        entry = float(prepared["Open"].iloc[entry_bar])
        stop = float(signal["stop_loss"])
        side = "buy" if signal["signal"] == "BUY" else "sell"
        if (side == "buy" and entry <= stop) or (side == "sell" and entry >= stop):
            continue
        risk = abs(entry - stop)
        risk_cash = cash * (float(risk_pct) / 100.0)
        if risk <= 0 or risk_cash <= 0 or cash <= 0:
            continue
        target = signal.get("target_1")
        signal_close = float(prepared["Close"].iloc[i])
        trade_zone = _trade_zone(prepared.iloc[: i + 1], timeframe, side, signal_close,
                                 zone_cache["zones"] if include_zones and zone_cache else None)
        position = {
            "zone": trade_zone,
            "side": side,
            "signal_index": i,
            "entry_index": entry_bar,
            "entry": entry,
            "stop": stop,
            "target": None if target is None else float(target),
            "target_2": signal.get("target_2"),
            "quantity": risk_cash / risk,
            "score": signal["strength"],
            "reasons": list(signal["reasons"]),
            "label": signal["label"],
            "mfe": 0.0,
            "mae": 0.0,
        }

    wins = [trade for trade in trades if (trade["pnl"] or 0) > 0]
    losses = [trade for trade in trades if (trade["pnl"] or 0) < 0]
    gross_profit = sum(trade["pnl"] for trade in wins)
    gross_loss = abs(sum(trade["pnl"] for trade in losses))
    win_rate = None if not trades else _num(100 * len(wins) / len(trades), 2)
    avg_win = None if not wins else _num(gross_profit / len(wins))
    avg_loss = None if not losses else _num(-gross_loss / len(losses))
    expectancy = None
    if trades and avg_win is not None and avg_loss is not None:
        expectancy = _num((len(wins) / len(trades)) * avg_win + (len(losses) / len(trades)) * avg_loss)
    elif trades and avg_win is not None:
        expectancy = avg_win
    elif trades and avg_loss is not None:
        expectancy = avg_loss
    reward_ratios = []
    for trade in trades:
        risk = abs((trade.get("entry") or 0) - (trade.get("stop_loss") or 0))
        reward = None if trade.get("target_1") is None else abs(trade["target_1"] - (trade.get("entry") or 0))
        if risk and reward is not None:
            reward_ratios.append(reward / risk)
    average_rr = None if not reward_ratios else _num(sum(reward_ratios) / len(reward_ratios))
    return {
        "disclaimer": "These figures describe this historical test only. They are not a guarantee of future performance.",
        "methodology": (
            "At bar N the signal is calculated from candles up to and including N. "
            "The order is filled at the next bar's open. "
            "A stop and a target on the same bar are booked as a stop. "
            "An opposite signal exits at the following bar's open. "
            "Position size risks the selected percent of current capital against the stop distance. "
            + (
                "Demand and supply are added only after the indicator score is already Buy or Sell, using candles up to that bar, and that zone pair is reused for at most five later bars."
                if include_zones else
                "This strategy does not use demand or supply. Stops use the prior 20-bar swing or one ATR."
            )
        ),
        "strategy": strategy,
        "timeframe": timeframe,
        "initial_capital": _num(capital),
        "risk_pct": risk_pct,
        "total_trades": len(trades),
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "win_rate": win_rate,
        "average_win": avg_win,
        "average_loss": avg_loss,
        "profit_factor": None if gross_loss == 0 else _num(gross_profit / gross_loss),
        "net_profit": _num(cash - float(capital)),
        "ending_capital": _num(cash),
        "max_drawdown_pct": _num(max_drawdown),
        "average_holding_bars": None if not trades else _num(sum(trade["holding_bars"] for trade in trades) / len(trades), 2),
        "risk_reward": average_rr,
        "expectancy": expectancy,
        "trades": trades,
        "equity": equity,
    }
