"""Chart backtest: historical entries, exits, targets and stops with reasons.

Rules that keep the walk free of look-ahead:
* Indicators are calculated once on the full candle history. Every indicator
  column is causal (row N depends only on rows up to N; a test checks this),
  so reading row N equals recalculating on candles up to N.
* Weekly and monthly trends at bar N combine the completed weeks or months
  with bar N's close for the week or month in progress.
* Demand and supply zones at bar N are detected on candles up to N only.
* A signal on the close of bar N fills at the open of bar N+1.
* Exit conditions seen on the close of bar N exit at the open of bar N+1.
* A stop and a target touched on the same bar are booked as the stop.

Positions are long only. Nothing here sends an order to a broker.
"""

import math

import numpy as np
import pandas as pd

from services.technical_analysis import (
    CHART_RESAMPLE,
    CONDITION_LABELS,
    ZONE_CONDITION_IDS,
    _candle_time,
    _fib_levels,
    _last_patterns,
    _pivots,
    _snapshot_values,
    add_indicators,
    clean_scan_params,
    condition_label,
    condition_report,
    condition_state,
    prepare_ohlcv,
)
from services import sd_zones

TARGET_METHODS = {
    "r_multiple": "Risk multiple",
    "atr": "ATR multiple",
    "percent": "Fixed percentage",
    "fibonacci": "Fibonacci level",
    "resistance": "Previous resistance",
    "supply": "Supply zone",
}
STOP_METHODS = {
    "swing_low": "Below swing low",
    "atr": "ATR below entry",
    "percent": "Percentage below entry",
    "demand": "Below demand zone",
    "fixed": "Fixed ₹ distance",
}
DEFAULT_TARGET_VALUES = {"r_multiple": [1.0, 2.0, 3.0], "atr": [1.0, 2.0, 3.0], "percent": [2.0, 4.0, 6.0]}
DEFAULT_STOP_VALUE = {"swing_low": 10, "atr": 1.5, "percent": 2.0, "demand": 0.25, "fixed": None}
EXIT_EVENTS = {
    "ema_cross_down": "EMA 20 crossed below EMA 50",
    "macd_cross_down": "MACD crossed below its signal line",
    "close_below_ema20": "Close below EMA 20",
    "supertrend_flip": "Supertrend turned bearish",
    "supply_reached": "Price reached the daily supply zone",
}
SUPPORTED_TIMEFRAMES = {"1d", "1wk", "1mo", "4h", "1h", "15m", "5m"}
ZONE_CHECK_BUDGET = 120
AUDIT_VALUE_KEYS = (
    "price", "rsi", "macd", "macd_signal", "macd_hist", "ema20", "ema50", "ema200",
    "sma50", "sma200", "adx", "atr", "volume", "volume_avg20", "rvol", "supertrend_dir",
    "bb_upper", "bb_lower",
)
EVENT_LABELS = {
    "breakout": "Breakout above the 20-bar high",
    "breakdown": "Breakdown below the 20-bar low",
    "ema_cross_up": "EMA 20 crossed above EMA 50",
    "ema_cross_down": "EMA 20 crossed below EMA 50",
    "volume_spike": "Volume above 2x the 20-bar average",
}
CANDLE_EVENTS = {
    "bullish_engulfing": "Bullish engulfing",
    "hammer": "Hammer",
    "morning_star": "Morning star",
    "bearish_engulfing": "Bearish engulfing",
    "shooting_star": "Shooting star",
}
MARKET_EVENT_LIMIT = 400
SAME_BAR_RULE = "A stop and a target touched on the same bar are booked as the stop."


def _num(value, places=2):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return round(number, places)


def _stamp_text(stamp, intraday):
    stamp = pd.Timestamp(stamp)
    return stamp.strftime("%d %b %Y %H:%M") if intraday else stamp.strftime("%d %b %Y")


class ConfigError(ValueError):
    pass


def _numbers(raw, count, name):
    if raw in (None, ""):
        return None
    if not isinstance(raw, (list, tuple)) or len(raw) != count:
        raise ConfigError(f"{name} needs {count} numbers")
    try:
        values = [float(item) for item in raw]
    except (TypeError, ValueError) as error:
        raise ConfigError(f"{name} must be numbers") from error
    if any(value <= 0 or math.isnan(value) for value in values):
        raise ConfigError(f"{name} must be positive")
    if values != sorted(values) or len(set(values)) != len(values):
        raise ConfigError(f"{name} must increase from target 1 to target 3")
    return values


def clean_config(raw, timeframe):
    """Validate a backtest request. Raises ConfigError with a readable message."""
    raw = raw or {}
    if timeframe not in SUPPORTED_TIMEFRAMES:
        raise ConfigError("Timeframe must be one of " + ", ".join(sorted(SUPPORTED_TIMEFRAMES)))
    entry = [str(item) for item in raw.get("entry_conditions") or []]
    if not entry:
        raise ConfigError("Choose at least one entry condition")
    unknown = [item for item in entry if item not in CONDITION_LABELS]
    if unknown:
        raise ConfigError("Unknown entry condition: " + ", ".join(unknown))
    entry_logic = str(raw.get("entry_logic") or "AND").upper()
    if entry_logic not in {"AND", "OR"}:
        raise ConfigError("Entry logic must be AND or OR")
    zone_entry = [item for item in entry if item in ZONE_CONDITION_IDS]
    if zone_entry and timeframe != "1d":
        raise ConfigError("Demand and supply conditions are calculated on daily candles only")
    if zone_entry and entry_logic == "OR":
        raise ConfigError("Demand and supply entry conditions need AND logic: each check recalculates zones on the candles up to that bar")
    exits = [str(item) for item in raw.get("exit_conditions") or []]
    bad_exit = [item for item in exits if item not in EXIT_EVENTS and (item not in CONDITION_LABELS or item in ZONE_CONDITION_IDS)]
    if bad_exit:
        raise ConfigError("Unsupported exit condition: " + ", ".join(bad_exit))
    if "supply_reached" in exits and timeframe != "1d":
        raise ConfigError("The supply-zone exit is calculated on daily candles only")
    target_method = str(raw.get("target_method") or "r_multiple")
    if target_method not in TARGET_METHODS:
        raise ConfigError("Unknown target method")
    if target_method == "supply" and timeframe != "1d":
        raise ConfigError("Supply-zone targets are calculated on daily candles only")
    target_values = None
    if target_method in DEFAULT_TARGET_VALUES:
        target_values = _numbers(raw.get("target_values"), 3, "Target values") or list(DEFAULT_TARGET_VALUES[target_method])
    exit_at = str(raw.get("exit_at") or "target_2")
    if exit_at not in {"target_1", "target_2", "target_3"}:
        raise ConfigError("Exit target must be target_1, target_2 or target_3")
    stop_method = str(raw.get("stop_method") or "swing_low")
    if stop_method not in STOP_METHODS:
        raise ConfigError("Unknown stop-loss method")
    if stop_method == "demand" and timeframe != "1d":
        raise ConfigError("Demand-zone stops are calculated on daily candles only")
    stop_value = raw.get("stop_value")
    if stop_value in (None, ""):
        stop_value = DEFAULT_STOP_VALUE[stop_method]
    if stop_value is None:
        raise ConfigError("Enter the ₹ distance for a fixed stop")
    try:
        stop_value = float(stop_value)
    except (TypeError, ValueError) as error:
        raise ConfigError("Stop value must be a number") from error
    limits = {"swing_low": (2, 100), "atr": (0.1, 10), "percent": (0.1, 50), "demand": (0, 5), "fixed": (0.01, 1e9)}
    low, high = limits[stop_method]
    if not low <= stop_value <= high:
        raise ConfigError(f"Stop value must be between {low:g} and {high:g}")
    if stop_method == "swing_low":
        stop_value = int(round(stop_value))
    trail = raw.get("trail_atr")
    if trail in (None, "", 0, "0"):
        trail = None
    else:
        try:
            trail = float(trail)
        except (TypeError, ValueError) as error:
            raise ConfigError("Trailing ATR must be a number") from error
        if not 0.1 <= trail <= 10:
            raise ConfigError("Trailing ATR must be between 0.1 and 10")
    return {
        "name": str(raw.get("name") or "Custom Technical Strategy")[:80],
        "entry_conditions": entry,
        "entry_logic": entry_logic,
        "exit_conditions": exits,
        "target_method": target_method,
        "target_values": target_values,
        "exit_at": exit_at,
        "stop_method": stop_method,
        "stop_value": stop_value,
        "trail_atr": trail,
        "zone_levels": bool(raw.get("zone_levels", True)) and timeframe == "1d",
        "params": clean_scan_params(raw.get("params")),
    }


def _trend_status(price, ema20, ema50):
    if price is None or ema20 is None or ema50 is None:
        return {"status": "Unavailable", "detail": "No close", "basis": "none"}
    if price > ema20 > ema50:
        return {"status": "Bullish", "detail": "Close above EMA 20 and EMA 50", "basis": "ema"}
    if price < ema20 < ema50:
        return {"status": "Bearish", "detail": "Close below EMA 20 and EMA 50", "basis": "ema"}
    return {"status": "Neutral", "detail": "EMA 20 and EMA 50 are mixed", "basis": "ema"}


def higher_timeframe_trends(prepared, timeframe):
    """Trend for every bar using completed weeks or months plus that bar's close.

    Equals trend_from_frame(resample_chart(candles up to N, timeframe)) because
    the EMA of the period in progress is alpha * close_N + (1 - alpha) * EMA of
    the previous completed period.
    """
    rule = CHART_RESAMPLE[timeframe]
    period = "W-SUN" if rule == "W" else "M"
    codes, _ = pd.factorize(prepared.index.to_period(period), sort=True)
    closes = prepared["Close"].to_numpy(dtype="float64")
    count = int(codes.max()) + 1 if len(codes) else 0
    final = np.full(count, np.nan)
    for position, code in enumerate(codes):
        final[code] = closes[position]
    ema = {}
    for span in (20, 50):
        alpha = 2.0 / (span + 1)
        series = np.full(count, np.nan)
        for index in range(count):
            series[index] = final[index] if index == 0 else alpha * final[index] + (1 - alpha) * series[index - 1]
        ema[span] = series
    trends = []
    for position, code in enumerate(codes):
        close = closes[position]
        values = []
        for span in (20, 50):
            alpha = 2.0 / (span + 1)
            values.append(close if code == 0 else alpha * close + (1 - alpha) * ema[span][code - 1])
        trends.append(_trend_status(_num(close, 4), _num(values[0], 4), _num(values[1], 4)))
    return trends


def zones_asof(prefix, timeframes=("1d", "1wk", "1mo")):
    """Nearest demand and supply up to the last bar, from the same engine as the live chart."""
    if prefix is None or len(prefix) < 35:
        return {key: {"demand": None, "supply": None} for key in ("1d", "1wk", "1mo")}
    return sd_zones.nearest_map(prepare_ohlcv(prefix), timeframes)


def bar_context(enriched, position, patterns, trends, params, include_vwap=False, zones=None, daily=True):
    """Scanner context for one bar, using only rows up to `position`."""
    values = _snapshot_values(enriched.iloc[max(0, position - 1):position + 1])
    if not values or values.get("price") is None:
        return None
    if values.get("atr") is not None and values.get("price"):
        values["atr_pct"] = _num(values["atr"] / values["price"] * 100)
    else:
        values["atr_pct"] = None
    values["vwap_slope"] = None
    if include_vwap and position >= 5 and "vwap" in enriched.columns:
        last, earlier = _num(enriched["vwap"].iloc[position], 4), _num(enriched["vwap"].iloc[position - 5], 4)
        values["vwap_slope"] = None if last is None or earlier is None else last - earlier
    if include_vwap and values.get("vwap") is not None:
        values["vwap_basis"] = "session"
    values["high_52w"] = None
    if daily and position + 1 >= 240:
        values["high_52w"] = _num(enriched["High"].iloc[max(0, position - 251):position + 1].max())
    return {
        "params": params,
        "values": values,
        "previous": _snapshot_values(enriched.iloc[max(0, position - 2):position]) if position > 0 else {},
        "trends": trends,
        "zones": zones or {key: {"demand": None, "supply": None} for key in ("1d", "1wk", "1mo")},
        "zones_calculated": zones is not None,
        "patterns": patterns[0],
        "fib": patterns[1],
        "vwap_enabled": include_vwap,
    }


def condition_value(condition_id, values):
    """Indicator reading behind a condition, for the reason text."""
    pick = {
        "rsi": ("RSI", values.get("rsi")),
        "macd": ("MACD", values.get("macd")),
        "adx": ("ADX", values.get("adx")),
    }
    if condition_id.startswith("rsi"):
        name, value = pick["rsi"]
        return None if value is None else f"{name} = {value:.1f}"
    if condition_id.startswith("macd"):
        macd, signal = values.get("macd"), values.get("macd_signal")
        return None if macd is None or signal is None else f"MACD {macd:.2f} vs signal {signal:.2f}"
    if condition_id.startswith("adx"):
        value = values.get("adx")
        return None if value is None else f"ADX = {value:.1f}"
    if condition_id in {"volume_gt_avg", "volume_gt_2x", "rvol_gt_1_5"}:
        volume, average = values.get("volume"), values.get("volume_avg20")
        if not volume or not average:
            return None
        return f"Volume = {volume / average:.2f}x the 20-bar average"
    if condition_id == "atr_pct_gt":
        value = values.get("atr_pct")
        return None if value is None else f"ATR = {value:.2f}% of close"
    keys = {
        "price_above_ema20": "ema20", "price_below_ema20": "ema20", "price_above_ema50": "ema50",
        "price_above_ema200": "ema200", "price_above_sma50": "sma50", "price_above_sma200": "sma200",
    }
    if condition_id in keys:
        level = values.get(keys[condition_id])
        price = values.get("price")
        return None if level is None or price is None else f"Close {price:,.2f} vs {keys[condition_id].upper()} {level:,.2f}"
    if condition_id == "ema20_gt_ema50":
        a, b = values.get("ema20"), values.get("ema50")
        return None if a is None or b is None else f"EMA 20 {a:,.2f} vs EMA 50 {b:,.2f}"
    if condition_id == "ema50_gt_ema200":
        a, b = values.get("ema50"), values.get("ema200")
        return None if a is None or b is None else f"EMA 50 {a:,.2f} vs EMA 200 {b:,.2f}"
    if condition_id == "sma50_gt_sma200":
        a, b = values.get("sma50"), values.get("sma200")
        return None if a is None or b is None else f"SMA 50 {a:,.2f} vs SMA 200 {b:,.2f}"
    if condition_id == "near_52w_high":
        high, price = values.get("high_52w"), values.get("price")
        return None if not high or price is None else f"Close {(high - price) / high * 100:.2f}% below the 52-week high {high:,.2f}"
    return None


def _report(condition_ids, context):
    report = condition_report(condition_ids, context)
    details = []
    for item in condition_ids:
        state = condition_state(item, context)
        details.append({
            "id": item,
            "label": condition_label(item, context.get("params")),
            "state": state,
            "value": condition_value(item, context["values"]),
        })
    report["details"] = details
    return report


def _distinct_above(levels, entry, count=3):
    chosen = []
    for price, label in sorted(levels, key=lambda item: item[0]):
        if price is None or price <= entry:
            continue
        if chosen and abs(price - chosen[-1][0]) <= max(entry * 0.001, 0.01):
            continue
        chosen.append((price, label))
        if len(chosen) == count:
            break
    return chosen


def calculate_targets(method, values, entry, risk, atr, window, zones):
    """Three target levels above the entry. Missing levels are None, never invented."""
    if method == "r_multiple":
        levels = [(entry + factor * risk, f"{factor:g}R = entry + {factor:g} x risk ({risk:,.2f})") for factor in values]
    elif method == "atr":
        if atr is None:
            levels = []
        else:
            levels = [(entry + factor * atr, f"entry + {factor:g} x ATR ({atr:,.2f})") for factor in values]
    elif method == "percent":
        levels = [(entry * (1 + pct / 100), f"entry + {pct:g}%") for pct in values]
    elif method == "fibonacci":
        fib = _fib_levels(window)
        raw = [(price, f"Fibonacci {ratio * 100:.1f}% of the last 120-bar swing") for ratio, price in fib.items()]
        if len(window):
            raw.append((float(window["High"].tail(120).max()), "Swing high of the last 120 bars"))
        levels = _distinct_above(raw, entry)
    elif method == "resistance":
        highs = window["High"].tail(120)
        pivots = _pivots(highs, "high")
        raw = [(float(highs.iloc[index]), f"Swing-high resistance from {pd.Timestamp(highs.index[index]).strftime('%d %b %Y')}") for index in pivots]
        levels = _distinct_above(raw, entry)
    elif method == "supply":
        raw = []
        names = {"1d": "Daily", "1wk": "Weekly", "1mo": "Monthly"}
        for timeframe in ("1d", "1wk", "1mo"):
            zone = (zones or {}).get(timeframe, {}).get("supply")
            if zone and zone.get("bottom") is not None:
                raw.append((float(zone["bottom"]), f"{names[timeframe]} supply zone low ({zone['bottom']:,.2f}–{zone['top']:,.2f})"))
        levels = _distinct_above(raw, entry)
    else:
        levels = []
    targets = []
    for slot in range(3):
        if slot < len(levels) and levels[slot][0] > entry:
            targets.append({"name": f"Target {slot + 1}", "price": _num(levels[slot][0]), "method": levels[slot][1]})
        else:
            targets.append({"name": f"Target {slot + 1}", "price": None, "method": "No level above entry"})
    return targets


def calculate_stop(method, value, entry, atr, window, zones):
    """(price, explanation) or (None, reason)."""
    if method == "swing_low":
        lows = window["Low"].tail(int(value))
        if lows.empty:
            return None, "No candles for the swing low"
        level = float(lows.min())
        return level, f"Lowest low of the last {int(value)} bars ({level:,.2f})"
    if method == "atr":
        if atr is None:
            return None, "ATR unavailable"
        return entry - value * atr, f"Entry - {value:g} x ATR ({atr:,.2f})"
    if method == "percent":
        return entry * (1 - value / 100), f"Entry - {value:g}%"
    if method == "fixed":
        return entry - value, f"Entry - ₹{value:,.2f}"
    if method == "demand":
        zone = (zones or {}).get("1d", {}).get("demand")
        if not zone or zone.get("bottom") is None:
            return None, "No daily demand zone below the entry"
        buffer = value * (atr or 0)
        level = float(zone["bottom"]) - buffer
        return level, f"Daily demand zone low {zone['bottom']:,.2f} - {value:g} x ATR"
    return None, "Unknown stop method"


def bar_events(enriched, patterns_at, position):
    """Technical events on the close of one bar."""
    found = []
    close = float(enriched["Close"].iloc[position])
    if position >= 20:
        prior = enriched.iloc[position - 20:position]
        if close > float(prior["High"].max()):
            found.append("breakout")
        if close < float(prior["Low"].min()):
            found.append("breakdown")
    if position >= 1:
        e20, e50 = enriched["ema20"].iloc[position], enriched["ema50"].iloc[position]
        p20, p50 = enriched["ema20"].iloc[position - 1], enriched["ema50"].iloc[position - 1]
        if pd.notna(e20) and pd.notna(e50) and pd.notna(p20) and pd.notna(p50):
            if e20 > e50 and p20 <= p50:
                found.append("ema_cross_up")
            if e20 < e50 and p20 >= p50:
                found.append("ema_cross_down")
    volume, average = enriched["Volume"].iloc[position], enriched["volume_avg20"].iloc[position]
    if pd.notna(volume) and pd.notna(average) and average > 0 and volume > 2 * average:
        found.append("volume_spike")
    patterns = patterns_at(position)[0]
    for key in CANDLE_EVENTS:
        if patterns.get(key) is True:
            found.append("candle:" + key)
    return found


def exit_event_state(name, enriched, position):
    if position < 1:
        return False
    row, prev = enriched.iloc[position], enriched.iloc[position - 1]
    if name == "ema_cross_down":
        return bool(pd.notna(row["ema20"]) and pd.notna(prev["ema20"]) and row["ema20"] < row["ema50"] and prev["ema20"] >= prev["ema50"])
    if name == "macd_cross_down":
        return bool(pd.notna(row["macd"]) and pd.notna(prev["macd"]) and row["macd"] < row["macd_signal"] and prev["macd"] >= prev["macd_signal"])
    if name == "close_below_ema20":
        return bool(pd.notna(row["ema20"]) and row["Close"] < row["ema20"])
    if name == "supertrend_flip":
        return bool(pd.notna(row["supertrend_dir"]) and pd.notna(prev["supertrend_dir"]) and row["supertrend_dir"] < 0 <= prev["supertrend_dir"])
    return False


def _event_label(kind):
    if kind.startswith("candle:"):
        return CANDLE_EVENTS[kind.split(":", 1)[1]]
    return EVENT_LABELS.get(kind, kind)


def _event_category(kind):
    if kind.startswith("candle:"):
        return "candlestick"
    if kind.startswith("ema_cross"):
        return "ema_cross"
    return kind


def build_markers(trades, market_events):
    """Chart markers derived only from the stored trades and events."""
    markers = []
    for trade in trades:
        markers.append({
            "time": trade["entry_time"], "kind": "entry", "trade": trade["trade"],
            "position": "belowBar", "shape": "arrowUp", "color": "#22c55e",
            "text": f"BUY {trade['entry']:,.2f}",
        })
        for event in trade.get("events") or []:
            if event["kind"] == "target":
                markers.append({
                    "time": event["time"], "kind": "target", "trade": trade["trade"],
                    "position": "aboveBar", "shape": "circle", "color": "#38bdf8",
                    "text": event["short"],
                })
        if trade.get("exit_time") is not None:
            stopped = "stop" in str(trade.get("exit_reason_code") or "")
            markers.append({
                "time": trade["exit_time"], "kind": "stop" if stopped else "exit", "trade": trade["trade"],
                "position": "aboveBar", "shape": "arrowDown",
                "color": "#f59e0b" if stopped else ("#ef4444" if (trade.get("pnl") or 0) < 0 else "#a78bfa"),
                "text": ("SL " if stopped else "EXIT ") + f"{trade['exit']:,.2f}",
            })
    for event in market_events:
        markers.append({
            "time": event["time"], "kind": event["category"], "trade": event.get("trade"),
            "position": "belowBar" if event["kind"] in {"breakout", "ema_cross_up", "candle:bullish_engulfing", "candle:hammer", "candle:morning_star"} else "aboveBar",
            "shape": "square" if event["category"] == "volume_spike" else "circle",
            "color": {"breakout": "#22c55e", "breakdown": "#ef4444", "ema_cross": "#eab308", "volume_spike": "#64748b", "candlestick": "#c084fc"}.get(event["category"], "#94a3b8"),
            "text": event["short"],
        })
    markers.sort(key=lambda item: (item["time"], item["kind"]))
    return markers


EVENT_SHORT = {
    "breakout": "BO", "breakdown": "BD", "ema_cross_up": "X↑", "ema_cross_down": "X↓", "volume_spike": "V",
}


def run_chart_backtest(frame, timeframe, config, symbol=None, start=None, end=None, capital=100000.0, risk_pct=1.0, progress=None):
    """Walk candles in order and return trades, markers, a timeline and an audit trail."""
    def report(stage, percent=None):
        if progress:
            progress(stage, percent)

    prepared = prepare_ohlcv(frame)
    if len(prepared) < 60:
        return {"data_unavailable": True, "message": "DATA UNAVAILABLE: fewer than 60 candles"}
    intraday = timeframe not in {"1d", "1wk", "1mo"}
    include_vwap = intraday
    report("Calculating indicators", 2)
    enriched = add_indicators(prepared, include_vwap=include_vwap)
    index = enriched.index
    first = 35
    if start:
        stamp = pd.Timestamp(start)
        first = max(first, int(index.searchsorted(stamp, side="left")))
    last = len(enriched) - 1
    if end:
        stamp = pd.Timestamp(end) + (pd.Timedelta(days=1) - pd.Timedelta(seconds=1) if len(str(end)) <= 10 else pd.Timedelta(0))
        last = min(last, int(index.searchsorted(stamp, side="right")) - 1)
    if last - first < 5:
        return {"data_unavailable": True, "message": "DATA UNAVAILABLE: the date range has too few candles"}

    params = config["params"]
    if timeframe == "1d":
        weekly = higher_timeframe_trends(prepared, "1wk")
        monthly = higher_timeframe_trends(prepared, "1mo")
    unavailable_trend = {"status": "Unavailable", "detail": "Higher-timeframe trend is calculated on daily candles only", "basis": "none"}

    def trends_at(position):
        row = enriched.iloc[position]
        own = _trend_status(_num(row["Close"], 4), _num(row["ema20"], 4), _num(row["ema50"], 4))
        if timeframe != "1d":
            return {"1d": unavailable_trend, "1wk": unavailable_trend, "1mo": unavailable_trend, "scan": own}
        return {"1d": own, "1wk": weekly[position], "1mo": monthly[position], "scan": own}

    pattern_cache = {}

    def patterns_at(position):
        if position not in pattern_cache:
            window = enriched.iloc[max(0, position - 129):position + 1]
            pattern_cache[position] = _last_patterns(window, _num(enriched["Close"].iloc[position]))
        return pattern_cache[position]

    zone_cache = {}
    zone_checks = {"count": 0}

    higher_zone_ids = {"weekly_demand", "weekly_supply", "monthly_demand", "monthly_supply"}
    zone_timeframes = ("1d", "1wk", "1mo") if (
        config["target_method"] == "supply" or higher_zone_ids & set(config["entry_conditions"])
    ) else ("1d",)

    def zones_at(position):
        if position not in zone_cache:
            zone_checks["count"] += 1
            zone_cache[position] = zones_asof(prepared.iloc[:position + 1], zone_timeframes)
        return zone_cache[position]

    def context_at(position, zones=None):
        return bar_context(enriched, position, patterns_at(position), trends_at(position), params, include_vwap, zones, timeframe == "1d")

    entry_ids = config["entry_conditions"]
    zone_ids = [item for item in entry_ids if item in ZONE_CONDITION_IDS]
    plain_ids = [item for item in entry_ids if item not in ZONE_CONDITION_IDS]
    needs_zone_levels = timeframe == "1d" and bool(
        config.get("zone_levels") or zone_ids or config["stop_method"] == "demand" or config["target_method"] == "supply"
        or "supply_reached" in config["exit_conditions"]
    )
    opens = enriched["Open"].to_numpy(dtype="float64")
    highs = enriched["High"].to_numpy(dtype="float64")
    lows = enriched["Low"].to_numpy(dtype="float64")
    closes = enriched["Close"].to_numpy(dtype="float64")
    atrs = enriched["atr"].to_numpy(dtype="float64")

    trades = []
    skipped = []
    market_events = []
    cash = float(capital)
    peak = cash
    max_drawdown = 0.0
    equity = []
    position = None
    span = max(1, last - first)

    def stamp(position_index):
        return _candle_time(index[position_index])

    def entry_signal(n):
        context = context_at(n)
        if context is None:
            return None, None
        states = [condition_state(item, context) for item in plain_ids]
        if config["entry_logic"] == "OR":
            if not any(state == "match" for state in states):
                return None, None
            return context, None
        if any(state != "match" for state in states):
            return None, None
        zones = None
        if zone_ids:
            if zone_checks["count"] >= ZONE_CHECK_BUDGET and n not in zone_cache:
                raise ConfigError(
                    f"More than {ZONE_CHECK_BUDGET} bars needed a demand/supply check. "
                    "Add another non-zone entry condition or shorten the date range."
                )
            zones = zones_at(n)
            context = context_at(n, zones)
            if not all(condition_state(item, context) == "match" for item in zone_ids):
                return None, None
        return context, zones

    def close_trade(exit_index, price, code, reasons):
        nonlocal cash, peak, max_drawdown, position
        qty = position["quantity"]
        pnl = qty * (price - position["entry"])
        cash += pnl
        peak = max(peak, cash)
        if peak:
            max_drawdown = max(max_drawdown, (peak - cash) / peak * 100)
        supply = (position["zones"] or {}).get("1d", {}).get("supply") if position["zones"] else None
        reasons = list(reasons)
        if supply and supply.get("bottom") is not None and float(supply["bottom"]) <= price:
            reasons.append(f"Exit price is inside or above the daily supply zone {supply['bottom']:,.2f}–{supply['top']:,.2f}")
        exit_time = stamp(exit_index)
        events = position["events"] + [{
            "time": exit_time, "date": _stamp_text(index[exit_index], intraday), "kind": "exit",
            "category": "stop" if "stop" in code else "exit", "short": "SL" if "stop" in code else "EXIT",
            "label": "Exit: " + "; ".join(reasons), "price": _num(price),
        }]
        holding_bars = int(exit_index - position["entry_index"])
        trade = {
            "trade": len(trades) + 1,
            "symbol": symbol,
            "timeframe": timeframe,
            "side": "BUY",
            "signal_time": stamp(position["signal_index"]),
            "signal_date": _stamp_text(index[position["signal_index"]], intraday),
            "signal_close": _num(closes[position["signal_index"]]),
            "time": stamp(position["signal_index"]),
            "entry_time": stamp(position["entry_index"]),
            "entry_date": _stamp_text(index[position["entry_index"]], intraday),
            "entry": _num(position["entry"]),
            "entry_basis": "Open of the bar after the signal",
            "stop_loss": _num(position["initial_stop"]),
            "stop_method": position["stop_method"],
            "final_stop": _num(position["stop"]),
            "risk": _num(position["risk"]),
            "targets": position["targets"],
            "target_1": position["targets"][0]["price"],
            "target_2": position["targets"][1]["price"],
            "target_3": position["targets"][2]["price"],
            "exit_target": position["exit_target_name"],
            "exit_time": exit_time,
            "exit_date": _stamp_text(index[exit_index], intraday),
            "exit": _num(price),
            "exit_reason": "; ".join(reasons),
            "exit_reasons": reasons,
            "exit_reason_code": code,
            "quantity": _num(qty, 4),
            "capped_by_cash": position["capped"],
            "pnl": _num(pnl),
            "pnl_pct": _num((price - position["entry"]) / position["entry"] * 100),
            "holding_bars": holding_bars,
            "holding_days": int((pd.Timestamp(index[exit_index]) - pd.Timestamp(index[position["entry_index"]])).days),
            "mfe": _num(position["max_high"] - position["entry"]),
            "mae": _num(position["entry"] - position["min_low"]),
            "matched": position["report"]["matched"],
            "failed": position["report"]["failed"],
            "unavailable": position["report"]["unavailable"],
            "conditions": position["report"]["details"],
            "indicator_values": position["indicator_values"],
            "zones": position["zones"],
            "zone_note": position["zone_note"],
            "demand_zone": (position["zones"] or {}).get("1d", {}).get("demand") if position["zones"] else None,
            "supply_zone": supply,
            "fib_levels": position["fib_levels"],
            "resistance_levels": position["resistance_levels"],
            "events": events,
        }
        trades.append(trade)
        equity.append({"time": exit_time, "equity": _num(cash), "drawdown_pct": _num((peak - cash) / peak * 100 if peak else 0)})
        position = None

    pending_exit = None
    for n in range(first, last + 1):
        if n % 25 == 0:
            report(f"Bar {n - first + 1} of {last - first + 1}", 5 + int(90 * (n - first) / span))
        events_here = bar_events(enriched, patterns_at, n)
        if position is not None:
            for kind in events_here:
                market_events.append({"time": stamp(n), "kind": kind, "trade": position["trade_no"]})
        else:
            for kind in events_here:
                market_events.append({"time": stamp(n), "kind": kind, "trade": None})

        if position is not None and n >= position["entry_index"]:
            o, h, l, c = opens[n], highs[n], lows[n], closes[n]
            exit_level = position["exit_level"]
            stop = position["stop"]
            stop_name = "trailing stop" if position["trailed"] else "stop loss"
            if pending_exit and n > position["entry_index"]:
                close_trade(n, o, "exit_condition", pending_exit)
                pending_exit = None
            elif o <= stop and n > position["entry_index"]:
                close_trade(n, o, "stop_gap", [f"{stop_name.capitalize()} {stop:,.2f}: the bar opened below it, filled at the open"])
            elif o >= exit_level and n > position["entry_index"]:
                close_trade(n, o, "target_gap", [f"{position['exit_target_name']} {exit_level:,.2f} reached: the bar opened above it, filled at the open"])
            elif l <= stop:
                same = h >= exit_level
                text = f"{stop_name.capitalize()} hit at {stop:,.2f}"
                if same:
                    text += " (target also touched on this bar; " + SAME_BAR_RULE[0].lower() + SAME_BAR_RULE[1:] + ")"
                close_trade(n, stop, "trailing_stop" if position["trailed"] else "stop", [text])
            else:
                position["max_high"] = max(position["max_high"], h)
                position["min_low"] = min(position["min_low"], l)
                for target in position["targets"]:
                    price = target["price"]
                    if price is None or target["name"] in position["touched"] or price >= exit_level:
                        continue
                    if h >= price:
                        position["touched"].add(target["name"])
                        position["events"].append({
                            "time": stamp(n), "date": _stamp_text(index[n], intraday), "kind": "target",
                            "category": "target", "short": "T" + target["name"][-1],
                            "label": f"{target['name']} reached ({target['method']})", "price": price,
                        })
                supply = position["supply"]
                if supply and not position["supply_seen"] and h >= float(supply["bottom"]):
                    position["supply_seen"] = True
                    position["events"].append({
                        "time": stamp(n), "date": _stamp_text(index[n], intraday), "kind": "supply",
                        "category": "supply", "short": "SZ",
                        "label": f"Price reached the daily supply zone {supply['bottom']:,.2f}–{supply['top']:,.2f}",
                        "price": _num(supply["bottom"]),
                    })
                if h >= exit_level:
                    position["events"].append({
                        "time": stamp(n), "date": _stamp_text(index[n], intraday), "kind": "target",
                        "category": "target", "short": "T" + position["exit_target_name"][-1],
                        "label": f"{position['exit_target_name']} reached ({position['exit_target_method']})",
                        "price": _num(exit_level),
                    })
                    close_trade(n, exit_level, "target", [f"{position['exit_target_name']} reached at {exit_level:,.2f} ({position['exit_target_method']})"])
                elif n == last:
                    close_trade(n, c, "end_of_test", ["End of the test window; the position was still open and is valued at the last close"])
                else:
                    for kind in events_here:
                        if kind in {"volume_spike", "breakout", "ema_cross_up", "ema_cross_down"}:
                            position["events"].append({
                                "time": stamp(n), "date": _stamp_text(index[n], intraday), "kind": "market",
                                "category": _event_category(kind), "short": EVENT_SHORT.get(kind, "•"),
                                "label": _event_label(kind), "price": _num(c),
                            })
                    if config["exit_conditions"]:
                        context = context_at(n)
                        met = []
                        for item in config["exit_conditions"]:
                            if item == "supply_reached":
                                if position["supply_seen"]:
                                    met.append(EXIT_EVENTS[item] + f" ({position['supply']['bottom']:,.2f})")
                            elif item in EXIT_EVENTS:
                                if exit_event_state(item, enriched, n):
                                    met.append(EXIT_EVENTS[item])
                            elif context is not None and condition_state(item, context) == "match":
                                met.append(condition_label(item, params))
                        if met:
                            pending_exit = [f"Exit condition on the close of {_stamp_text(index[n], intraday)}: " + item for item in met]
                    if position is not None and config["trail_atr"] and not math.isnan(atrs[n]):
                        candidate = c - config["trail_atr"] * atrs[n]
                        if candidate > position["stop"]:
                            position["stop"] = candidate
                            position["trailed"] = True

        if position is not None or n >= last:
            continue
        signal_context, zones = entry_signal(n)
        if signal_context is None:
            continue
        entry_index = n + 1
        entry = float(opens[entry_index])
        atr = None if math.isnan(atrs[n]) else float(atrs[n])
        window = enriched.iloc[max(0, n - 129):n + 1]
        zone_note = None
        if needs_zone_levels and zones is None:
            if zone_checks["count"] < ZONE_CHECK_BUDGET:
                zones = zones_at(n)
                signal_context = context_at(n, zones)
            else:
                zone_note = f"Demand/supply zones not calculated for this trade: the limit of {ZONE_CHECK_BUDGET} zone checks per run was reached"
                if config["stop_method"] == "demand" or config["target_method"] == "supply":
                    skipped.append({"time": stamp(n), "date": _stamp_text(index[n], intraday), "reason": zone_note})
                    continue
        elif not needs_zone_levels:
            zone_note = "Demand/supply zones were not requested" if timeframe == "1d" else "Demand/supply zones are calculated on daily candles only"
        stop, stop_text = calculate_stop(config["stop_method"], config["stop_value"], entry, atr, window, zones)
        if stop is None or stop >= entry:
            skipped.append({"time": stamp(n), "date": _stamp_text(index[n], intraday), "reason": stop_text if stop is None else f"Stop {stop:,.2f} is not below the entry {entry:,.2f}"})
            continue
        risk = entry - stop
        targets = calculate_targets(config["target_method"], config["target_values"], entry, risk, atr, window, zones)
        wanted = int(config["exit_at"][-1])
        available = [target for target in targets[:wanted] if target["price"] is not None]
        if not available:
            skipped.append({"time": stamp(n), "date": _stamp_text(index[n], intraday), "reason": "No target level above the entry"})
            continue
        exit_target = available[-1]
        budget = cash * float(risk_pct) / 100.0
        quantity = budget / risk
        capped = False
        if quantity * entry > cash:
            quantity = cash / entry
            capped = True
        if quantity <= 0:
            skipped.append({"time": stamp(n), "date": _stamp_text(index[n], intraday), "reason": "No cash left"})
            continue
        report_data = _report(entry_ids, signal_context)
        values = signal_context["values"]
        fib = _fib_levels(window)
        pivot_highs = window["High"].tail(120)
        resistance = sorted({_num(pivot_highs.iloc[i]) for i in _pivots(pivot_highs, "high")})
        supply_zone = (zones or {}).get("1d", {}).get("supply") if zones else None
        position = {
            "trade_no": len(trades) + 1,
            "signal_index": n,
            "entry_index": entry_index,
            "entry": entry,
            "stop": stop,
            "initial_stop": stop,
            "stop_method": stop_text,
            "trailed": False,
            "risk": risk,
            "targets": targets,
            "exit_level": float(exit_target["price"]),
            "exit_target_name": exit_target["name"],
            "exit_target_method": exit_target["method"],
            "quantity": quantity,
            "capped": capped,
            "report": report_data,
            "indicator_values": {key: values.get(key) for key in AUDIT_VALUE_KEYS},
            "zones": zones,
            "zone_note": zone_note,
            "supply": supply_zone if supply_zone and supply_zone.get("bottom") is not None and float(supply_zone["bottom"]) > entry else None,
            "supply_seen": False,
            "fib_levels": [{"ratio": ratio, "price": _num(price)} for ratio, price in fib.items()],
            "resistance_levels": resistance,
            "touched": set(),
            "max_high": entry,
            "min_low": entry,
            "events": [{
                "time": stamp(entry_index), "date": _stamp_text(index[entry_index], intraday), "kind": "entry",
                "category": "entry", "short": "BUY",
                "label": f"Entry at the open after the signal on {_stamp_text(index[n], intraday)}",
                "price": _num(entry),
            }],
        }

    report("Building markers", 97)
    for event in market_events:
        event["category"] = _event_category(event["kind"])
        event["label"] = _event_label(event["kind"])
        event["short"] = EVENT_SHORT.get(event["kind"], "C")
        event["date"] = _stamp_text(pd.Timestamp(event["time"], unit="s"), intraday)
    capped_events = len(market_events) > MARKET_EVENT_LIMIT
    market_events = market_events[-MARKET_EVENT_LIMIT:]
    markers = build_markers(trades, market_events)
    audit = [audit_record(trade) for trade in trades]
    timeline = []
    for trade in trades:
        for event in trade["events"]:
            timeline.append(dict(event, trade=trade["trade"]))
    timeline.sort(key=lambda item: (item["time"], item["trade"]))

    wins = [trade for trade in trades if (trade["pnl"] or 0) > 0]
    losses = [trade for trade in trades if (trade["pnl"] or 0) < 0]
    gross_profit = sum(trade["pnl"] for trade in wins)
    gross_loss = abs(sum(trade["pnl"] for trade in losses))
    report("Checking the latest bar", 98)
    latest_zones = (lambda position: zone_cache.get(position) or zones_asof(prepared.iloc[:position + 1], zone_timeframes)) if needs_zone_levels else None
    current = current_conditions(enriched, prepared, last, context_at, latest_zones, config, trades)
    report("Complete", 100)
    return {
        "strategy": "chart",
        "strategy_name": config["name"],
        "symbol": symbol,
        "timeframe": timeframe,
        "config": config,
        "date_from": _stamp_text(index[first], intraday),
        "date_to": _stamp_text(index[last], intraday),
        "first_time": stamp(first),
        "last_time": stamp(last),
        "initial_capital": _num(capital),
        "risk_pct": _num(risk_pct),
        "ending_capital": _num(cash),
        "net_profit": _num(cash - capital),
        "total_trades": len(trades),
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "win_rate": None if not trades else _num(len(wins) / len(trades) * 100),
        "profit_factor": None if not gross_loss else _num(gross_profit / gross_loss),
        "max_drawdown_pct": _num(max_drawdown),
        "average_holding_bars": None if not trades else _num(sum(t["holding_bars"] for t in trades) / len(trades)),
        "expectancy": None if not trades else _num(sum(t["pnl"] for t in trades) / len(trades)),
        "trades": trades,
        "equity": equity,
        "markers": markers,
        "market_events": market_events,
        "market_events_capped": capped_events,
        "timeline": timeline,
        "audit": audit,
        "skipped": skipped[-50:],
        "zone_checks": zone_checks["count"],
        "current": current,
        "methodology": (
            "Signals use candles up to and including the signal bar. Entries fill at the next bar's open. "
            + SAME_BAR_RULE + " A bar that opens beyond the stop or the exit target fills at that open. "
            "Exit conditions seen on a close exit at the next open. Demand and supply zones at a signal come from the "
            "same zone engine as the live chart, run on candles up to that bar only. "
            "Long positions only. One position at a time."
        ),
        "disclaimer": "These figures describe this historical test only. They are not a guarantee of future performance.",
    }


def audit_record(trade):
    return {
        "stock": trade["symbol"],
        "timeframe": trade["timeframe"],
        "timestamp": trade["signal_time"],
        "signal_date": trade["signal_date"],
        "price": trade["entry"],
        "signal_close": trade["signal_close"],
        "entry_time": trade["entry_time"],
        "signal_type": "ENTRY",
        "triggered_conditions": trade["matched"],
        "not_triggered_conditions": trade["failed"],
        "unavailable_conditions": trade["unavailable"],
        "indicator_values": trade["indicator_values"],
        "demand_zone": trade["demand_zone"],
        "supply_zone": trade["supply_zone"],
        "zone_note": trade.get("zone_note"),
        "target": [{"name": item["name"], "price": item["price"], "method": item["method"]} for item in trade["targets"]],
        "stop_loss": {"price": trade["stop_loss"], "method": trade["stop_method"]},
        "exit_time": trade["exit_time"],
        "exit_price": trade["exit"],
        "exit_reason": trade["exit_reason"],
    }


def current_conditions(enriched, prepared, last, context_at, zones_at, config, trades):
    """What is true on the latest bar, and which predefined conditions to watch.

    This is condition monitoring, not a forecast and not a buy or sell call.
    """
    zones = zones_at(last) if zones_at else None
    context = context_at(last, zones)
    if context is None:
        return None
    values = context["values"]
    report = _report(config["entry_conditions"], context)
    watch = []
    for detail in report["details"]:
        if detail["state"] == "fail":
            watch.append({"label": detail["label"], "detail": detail["value"] or "Not true on the latest close", "source": "entry rule"})
    ema20, ema50, price = values.get("ema20"), values.get("ema50"), values.get("price")
    if ema20 is not None and ema50 is not None and ema20 <= ema50:
        watch.append({"label": "EMA 20 / EMA 50 bullish crossover", "detail": f"EMA 20 is {(ema50 - ema20) / ema50 * 100:.2f}% below EMA 50", "source": "monitor"})
    if len(enriched) > 21 and price is not None:
        level = float(enriched["High"].iloc[last - 20:last].max())
        if price <= level:
            watch.append({"label": "Breakout above the 20-bar high", "detail": f"Level {level:,.2f}, {(level - price) / price * 100:.2f}% above the close", "source": "monitor"})
    volume, average = values.get("volume"), values.get("volume_avg20")
    multiple = (config["params"] or {}).get("vol_mult") or 1.5
    if volume is not None and average:
        if volume <= multiple * average:
            watch.append({"label": f"Volume confirmation (above {multiple:g}x average)", "detail": f"Latest volume is {volume / average:.2f}x the 20-bar average", "source": "monitor"})
    rsi = values.get("rsi")
    if rsi is not None and rsi < 70:
        watch.append({"label": "RSI above 70", "detail": f"RSI is {rsi:.1f}", "source": "monitor"})
    supply = (zones or {}).get("1d", {}).get("supply") if zones else None
    if supply and price is not None:
        watch.append({"label": "Daily supply zone", "detail": f"{supply['bottom']:,.2f}–{supply['top']:,.2f}, {supply.get('distance_pct')}% from the close", "source": "monitor"})
    open_trade = trades[-1] if trades and trades[-1].get("exit_reason_code") == "end_of_test" else None
    return {
        "time": _candle_time(enriched.index[last]),
        "price": price,
        "matched": [detail for detail in report["details"] if detail["state"] == "match"],
        "unavailable": [detail for detail in report["details"] if detail["state"] == "unavailable"],
        "watch": watch,
        "open_position": None if not open_trade else {
            "entry": open_trade["entry"], "entry_date": open_trade["entry_date"],
            "stop_loss": open_trade["final_stop"], "targets": open_trade["targets"],
        },
        "zones": zones,
        "note": "Conditions to watch are monitored rules, not a prediction or a buy/sell signal.",
    }
