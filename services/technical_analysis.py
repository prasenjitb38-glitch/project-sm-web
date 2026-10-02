"""Calculated technicals for the Stock Analysis view.

Indicators are derived from the OHLCV frame the caller supplies. Missing
history stays null. This module does not invent prices or scanner hits.
Demand and supply zones come from services.zone_engine.
"""

import logging
import math
import threading
import time
from collections import OrderedDict

import pandas as pd

log = logging.getLogger("sm.technical")

# Defaults used by add_indicators. Scanner thresholds that are not listed here
# are fixed in condition_state and are not a user-selected parameter.
INDICATOR_DEFAULTS = {
    "sma": [5, 10, 20, 50, 100, 200],
    "ema": [9, 20, 50, 100, 200],
    "rsi_period": 14,
    "macd": {"fast": 12, "slow": 26, "signal": 9},
    "stochastic": {"k": 14, "d": 3},
    "williams_period": 14,
    "cci_period": 20,
    "roc_period": 12,
    "momentum_period": 10,
    "adx_period": 14,
    "atr_period": 14,
    "bollinger": {"period": 20, "stdev": 2, "ddof": 0},
    "supertrend": {"period": 10, "multiplier": 3},
    "psar": {"step": 0.02, "maximum": 0.20},
    "ichimoku": {"tenkan": 9, "kijun": 26, "senkou": 52},
    "donchian_period": 20,
    "keltner": {"period": 20, "atr": 14, "multiplier": 2},
    "volume_average": 20,
    "vwap": "session, intraday only, requires at least 2 bars in the session",
}

from services import sd_zones
from services.zone_cache import check_superseded

# Same resample rules as the existing chart route, so overlay times match candles.
CHART_RESAMPLE = {
    "1wk": "W",
    "1mo": "ME",
    "3mo": "3ME",
    "6mo": "6ME",
    "1y": "YE",
    "5y": "5YE",
}

SCAN_CATALOG = [
    {
        "id": "price_trend",
        "label": "Price & Trend",
        "conditions": [
            {"id": "higher_high_low", "label": "Higher high and higher low (last 20 bars)"},
            {"id": "near_52w_high", "label": "Close within the chosen % of the 52-week high", "param": {"key": "near_52w_pct", "default": 5, "unit": "%"}},
            {"id": "price_above_sma200", "label": "Price above SMA 200"},
            {"id": "price_above_ema50", "label": "Price above EMA 50"},
            {"id": "price_above_ema200", "label": "Price above EMA 200"},
            {"id": "price_below_ema20", "label": "Price below EMA 20"},
        ],
    },
    {
        "id": "moving_averages",
        "label": "Moving Averages",
        "conditions": [
            {"id": "ema20_gt_ema50", "label": "EMA 20 above EMA 50"},
            {"id": "sma50_gt_sma200", "label": "SMA 50 above SMA 200"},
            {"id": "price_above_ema20", "label": "Price above EMA 20"},
            {"id": "ema50_gt_ema200", "label": "EMA 50 above EMA 200"},
            {"id": "price_above_sma50", "label": "Price above SMA 50"},
            {"id": "golden_cross", "label": "Golden cross (SMA 50 crossed above SMA 200 on this bar)"},
            {"id": "death_cross", "label": "Death cross (SMA 50 crossed below SMA 200 on this bar)"},
        ],
    },
    {
        "id": "momentum",
        "label": "Momentum",
        "conditions": [
            {"id": "rsi_50_70", "label": "RSI inside the chosen range", "param": {"key": "rsi_min", "default": 50, "key2": "rsi_max", "default2": 70}},
            {"id": "macd_above_signal", "label": "MACD above signal"},
            {"id": "adx_gt_25", "label": "ADX above the chosen value", "param": {"key": "adx_min", "default": 25}},
            {"id": "rsi_above_50", "label": "RSI above 50"},
            {"id": "rsi_oversold", "label": "RSI below 30"},
            {"id": "rsi_overbought", "label": "RSI above 70"},
            {"id": "macd_hist_positive", "label": "MACD histogram positive"},
        ],
    },
    {
        "id": "volume",
        "label": "Volume",
        "conditions": [
            {"id": "volume_gt_avg", "label": "Volume above the 20-bar average times the chosen multiple", "param": {"key": "vol_mult", "default": 1, "unit": "x"}},
            {"id": "rvol_gt_1_5", "label": "Relative volume above the chosen value", "param": {"key": "rvol_min", "default": 1.5, "unit": "x"}},
            {"id": "volume_gt_2x", "label": "Volume above 2x the 20-bar average"},
            {"id": "price_above_vwap", "label": "Price above session VWAP"},
            {"id": "price_below_vwap", "label": "Price below session VWAP"},
            {"id": "vwap_bullish", "label": "VWAP bullish (price above a rising session VWAP)"},
            {"id": "vwap_bearish", "label": "VWAP bearish (price below a falling session VWAP)"},
        ],
    },
    {
        "id": "volatility",
        "label": "Volatility",
        "conditions": [
            {"id": "atr_pct_gt", "label": "ATR 14 as a % of close above the chosen value", "param": {"key": "atr_pct", "default": 2, "unit": "%"}},
            {"id": "above_upper_bb", "label": "Close above upper Bollinger Band"},
            {"id": "below_lower_bb", "label": "Close below lower Bollinger Band"},
            {"id": "inside_bb", "label": "Close inside Bollinger Bands"},
        ],
    },
    {
        "id": "trend_indicators",
        "label": "Trend Indicators",
        "conditions": [
            {"id": "supertrend_bullish", "label": "Supertrend bullish"},
            {"id": "price_above_cloud", "label": "Close above the Ichimoku cloud"},
            {"id": "supertrend_bearish", "label": "Supertrend bearish"},
            {"id": "price_below_cloud", "label": "Close below the Ichimoku cloud"},
        ],
    },
    {
        "id": "price_action",
        "label": "Price Action",
        "conditions": [
            {"id": "bullish_engulfing", "label": "Bullish engulfing"},
            {"id": "hammer", "label": "Hammer"},
            {"id": "morning_star", "label": "Morning star"},
            {"id": "bullish_candle", "label": "Last candle bullish"},
            {"id": "bearish_candle", "label": "Last candle bearish"},
            {"id": "bearish_engulfing", "label": "Bearish engulfing"},
            {"id": "inverted_hammer", "label": "Inverted hammer"},
            {"id": "shooting_star", "label": "Shooting star"},
            {"id": "hanging_man", "label": "Hanging man"},
            {"id": "doji", "label": "Doji"},
            {"id": "evening_star", "label": "Evening star"},
            {"id": "piercing", "label": "Piercing pattern"},
            {"id": "dark_cloud", "label": "Dark cloud cover"},
            {"id": "inside_bar", "label": "Inside bar"},
        ],
    },
    {
        "id": "chart_patterns",
        "label": "Chart Patterns",
        "conditions": [
            {"id": "breakout_candle", "label": "Breakout (close above the prior 20-bar high)"},
            {"id": "double_bottom", "label": "Double bottom, close above the neckline"},
            {"id": "double_top", "label": "Double top, close below the neckline"},
            {"id": "breakdown_candle", "label": "Breakdown (close below the prior 20-bar low)"},
        ],
    },
    {
        "id": "fibonacci",
        "label": "Fibonacci",
        "conditions": [
            {"id": "fib_golden_zone", "label": "Close between the 38.2% and 61.8% retracement"},
            {"id": "near_fib_618", "label": "Price within 0.8% of the 61.8% retracement"},
            {"id": "near_fib_382", "label": "Price within 0.8% of the 38.2% retracement"},
            {"id": "near_fib_500", "label": "Price within 0.8% of the 50% retracement"},
            {"id": "near_fib_236", "label": "Price within 0.8% of the 23.6% retracement"},
            {"id": "near_fib_786", "label": "Price within 0.8% of the 78.6% retracement"},
        ],
    },
    {
        "id": "supply_demand",
        "label": "Supply & Demand",
        "conditions": [
            {"id": "daily_demand", "label": "At or just above a daily demand zone"},
            {"id": "weekly_demand", "label": "At or just above a weekly demand zone"},
            {"id": "monthly_demand", "label": "At or just above a monthly demand zone"},
            {"id": "daily_supply", "label": "At or just below a daily supply zone"},
            {"id": "weekly_supply", "label": "At or just below a weekly supply zone"},
            {"id": "monthly_supply", "label": "At or just below a monthly supply zone"},
        ],
    },
    {
        "id": "multi_timeframe",
        "label": "Multi-Timeframe",
        "conditions": [
            {"id": "monthly_bullish", "label": "Monthly bullish"},
            {"id": "weekly_bullish", "label": "Weekly bullish"},
            {"id": "daily_bullish", "label": "Daily bullish"},
            {"id": "mtf_daily_weekly_bullish", "label": "Daily and weekly both bullish"},
            {"id": "mtf_all_bullish", "label": "Daily, weekly, and monthly all bullish"},
            {"id": "daily_bearish", "label": "Daily bearish"},
            {"id": "weekly_bearish", "label": "Weekly bearish"},
            {"id": "monthly_bearish", "label": "Monthly bearish"},
        ],
    },
    {
        "id": "other",
        "label": "Other Filters",
        "conditions": [
            {"id": "price_range", "label": "Close inside the chosen price range", "param": {"key": "price_min", "default": None, "key2": "price_max", "default2": None}},
            {"id": "change_positive", "label": "Last bar change positive"},
            {"id": "change_negative", "label": "Last bar change negative"},
        ],
    },
]

SCAN_PARAM_DEFAULTS = {
    item["param"][key]: item["param"].get("default" if key == "key" else "default2")
    for group in SCAN_CATALOG
    for item in group["conditions"]
    if "param" in item
    for key in ("key", "key2")
    if key in item["param"]
}


def clean_scan_params(raw):
    """Numeric scanner parameters. Unknown keys and non-numbers are dropped."""
    params = dict(SCAN_PARAM_DEFAULTS)
    for key, value in (raw or {}).items():
        if key not in SCAN_PARAM_DEFAULTS or value in (None, ""):
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            params[key] = number
    return params


CONDITION_LABELS = {
    item["id"]: item["label"]
    for group in SCAN_CATALOG
    for item in group["conditions"]
}

SCAN_TIMEFRAMES = ("5m", "15m", "1h", "4h", "1d", "1wk", "1mo")
INTRADAY_SCAN = {"5m", "15m", "1h", "4h"}
FIB_RATIOS = (0.236, 0.382, 0.5, 0.618, 0.786)
ALERT_TYPES = (
    "demand_entry",
    "supply_entry",
    "ema_cross",
    "rsi_threshold",
    "macd_cross",
    "supertrend_change",
    "vwap_cross",
    "score_threshold",
    "buy_signal",
    "sell_signal",
    "breakout",
    "breakdown",
    "volume_breakout",
    "target_reached",
    "stop_reached",
)


def _num(value, digits=2):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return round(number, digits)


def prepare_ohlcv(df):
    """Return a sorted numeric OHLCV frame with a naive datetime index."""
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    frame = df.copy()
    if not isinstance(frame.index, pd.DatetimeIndex):
        frame.index = pd.to_datetime(frame.index, errors="coerce")
    if getattr(frame.index, "tz", None) is not None:
        frame.index = frame.index.tz_convert("Asia/Kolkata").tz_localize(None)
    for column in ("Open", "High", "Low", "Close"):
        if column not in frame.columns:
            frame[column] = pd.NA
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    if "Volume" not in frame.columns:
        frame["Volume"] = 0
    frame["Volume"] = pd.to_numeric(frame["Volume"], errors="coerce").fillna(0)
    frame = frame.dropna(subset=["Open", "High", "Low", "Close"]).sort_index()
    frame = frame[~frame.index.duplicated(keep="last")]
    valid = (
        (frame["High"] >= frame["Open"])
        & (frame["High"] >= frame["Close"])
        & (frame["High"] >= frame["Low"])
        & (frame["Low"] <= frame["Open"])
        & (frame["Low"] <= frame["Close"])
        & (frame["Volume"] >= 0)
    )
    rejected = int((~valid).sum())
    if rejected:
        log.warning("OHLC_VALIDATION_REJECT count=%s", rejected)
        frame = frame.loc[valid]
    return frame[["Open", "High", "Low", "Close", "Volume"]]


def resample_chart(df, timeframe):
    """Match the candles drawn by /api/chart for a higher timeframe."""
    frame = prepare_ohlcv(df)
    rule = CHART_RESAMPLE.get(timeframe)
    if frame.empty or not rule:
        return frame
    return frame.resample(rule).agg({
        "Open": "first",
        "High": "max",
        "Low": "min",
        "Close": "last",
        "Volume": "sum",
    }).dropna(subset=["Open", "High", "Low", "Close"])


def _rma(series, period):
    values = pd.to_numeric(series, errors="coerce").to_numpy(dtype=float)
    out = [math.nan] * len(values)
    start = None
    for index in range(period - 1, len(values)):
        window = values[index - period + 1:index + 1]
        if all(math.isfinite(float(item)) for item in window):
            start = index
            break
    if start is None:
        return pd.Series(out, index=series.index)
    previous = sum(values[start - period + 1:start + 1]) / period
    out[start] = previous
    for index in range(start + 1, len(values)):
        value = values[index]
        if not math.isfinite(value):
            out[index] = previous
            continue
        previous = (previous * (period - 1) + value) / period
        out[index] = previous
    return pd.Series(out, index=series.index)


def _wilder_atr(frame, period):
    previous_close = frame["Close"].shift(1)
    true_range = pd.concat([
        frame["High"] - frame["Low"],
        (frame["High"] - previous_close).abs(),
        (frame["Low"] - previous_close).abs(),
    ], axis=1).max(axis=1)
    return _rma(true_range, period)


def _dmi(frame, period=14):
    """Wilder +DI, -DI, and ADX. Returns are aligned to the frame index."""
    up_move = frame["High"].diff()
    down_move = -frame["Low"].diff()
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)
    atr = _wilder_atr(frame, period).replace(0, math.nan)
    plus_di = 100 * _rma(plus_dm, period) / atr
    minus_di = 100 * _rma(minus_dm, period) / atr
    denom = (plus_di + minus_di).replace(0, math.nan)
    dx = 100 * (plus_di - minus_di).abs() / denom
    return plus_di, minus_di, _rma(dx, period)


def _adx(frame, period=14):
    return _dmi(frame, period)[2]


def _psar(frame, step=0.02, maximum=0.20):
    """Wilder parabolic SAR. Rising SAR is capped by the prior two lows."""
    high = frame["High"].to_numpy(dtype=float)
    low = frame["Low"].to_numpy(dtype=float)
    close = frame["Close"].to_numpy(dtype=float)
    size = len(frame)
    sar = [math.nan] * size
    trend = [math.nan] * size
    if size < 2:
        return pd.Series(sar, index=frame.index), pd.Series(trend, index=frame.index)
    rising = close[1] > close[0]
    trend[0] = 1 if rising else -1
    sar[0] = low[0] if rising else high[0]
    extreme = high[0] if rising else low[0]
    af = step
    for index in range(1, size):
        candidate = sar[index - 1] + af * (extreme - sar[index - 1])
        if trend[index - 1] == 1:
            prior = low[index - 1]
            older = low[index - 2] if index >= 2 else prior
            candidate = min(candidate, prior, older)
            if low[index] < candidate:
                trend[index] = -1
                sar[index] = extreme
                extreme = low[index]
                af = step
            else:
                trend[index] = 1
                sar[index] = candidate
                if high[index] > extreme:
                    extreme = high[index]
                    af = min(maximum, af + step)
        else:
            prior = high[index - 1]
            older = high[index - 2] if index >= 2 else prior
            candidate = max(candidate, prior, older)
            if high[index] > candidate:
                trend[index] = 1
                sar[index] = extreme
                extreme = high[index]
                af = step
            else:
                trend[index] = -1
                sar[index] = candidate
                if low[index] < extreme:
                    extreme = low[index]
                    af = min(maximum, af + step)
    return pd.Series(sar, index=frame.index), pd.Series(trend, index=frame.index)


def _supertrend(frame, period=10, multiplier=3):
    atr = _wilder_atr(frame, period)
    hl2 = (frame["High"] + frame["Low"]) / 2
    basic_upper = hl2 + multiplier * atr
    basic_lower = hl2 - multiplier * atr
    size = len(frame)
    final_upper = [math.nan] * size
    final_lower = [math.nan] * size
    trend = [math.nan] * size
    line = [math.nan] * size
    closes = frame["Close"].to_numpy(dtype=float)
    for index in range(size):
        upper = basic_upper.iloc[index]
        lower = basic_lower.iloc[index]
        if not math.isfinite(upper) or not math.isfinite(lower):
            continue
        if index == 0 or not math.isfinite(final_upper[index - 1]):
            final_upper[index] = float(upper)
            final_lower[index] = float(lower)
            trend[index] = 1
        else:
            previous_close = closes[index - 1]
            final_upper[index] = float(upper) if (
                upper < final_upper[index - 1] or previous_close > final_upper[index - 1]
            ) else final_upper[index - 1]
            final_lower[index] = float(lower) if (
                lower > final_lower[index - 1] or previous_close < final_lower[index - 1]
            ) else final_lower[index - 1]
            if trend[index - 1] == 1:
                trend[index] = -1 if closes[index] < final_lower[index] else 1
            else:
                trend[index] = 1 if closes[index] > final_upper[index] else -1
        line[index] = final_lower[index] if trend[index] == 1 else final_upper[index]
    return pd.Series(line, index=frame.index), pd.Series(trend, index=frame.index)


def _rolling_mean_deviation(series, period):
    values = pd.to_numeric(series, errors="coerce").to_numpy(dtype=float)
    out = [math.nan] * len(values)
    for index in range(period - 1, len(values)):
        window = values[index - period + 1:index + 1]
        if not all(math.isfinite(float(item)) for item in window):
            continue
        mean = sum(window) / period
        out[index] = sum(abs(item - mean) for item in window) / period
    return pd.Series(out, index=series.index)


def _session_vwap(frame):
    """Intraday session VWAP. One-bar daily frames are not treated as VWAP."""
    if frame.empty or not isinstance(frame.index, pd.DatetimeIndex):
        return pd.Series(dtype=float)
    dates = frame.index.normalize()
    counts = dates.value_counts()
    if int(counts.max()) < 2:
        return pd.Series(math.nan, index=frame.index)
    typical = (frame["High"] + frame["Low"] + frame["Close"]) / 3
    volume = frame["Volume"].where(frame["Volume"] > 0)
    price_volume = typical * volume
    cumulative_price = price_volume.groupby(dates).cumsum()
    cumulative_volume = volume.groupby(dates).cumsum().replace(0, math.nan)
    vwap = cumulative_price / cumulative_volume
    short_session = dates.map(counts) < 2
    return vwap.mask(short_session)


_INDICATOR_CACHE = OrderedDict()
_INDICATOR_LOCK = threading.Lock()
_INDICATOR_LIMIT = 24


def add_indicators(df, include_vwap=False):
    check_superseded()
    try:
        key = (bool(include_vwap), len(df), tuple(df.columns), int(pd.util.hash_pandas_object(df, index=True).sum()))
    except Exception:
        key = None
    if key is not None:
        with _INDICATOR_LOCK:
            hit = _INDICATOR_CACHE.get(key)
            if hit is not None:
                _INDICATOR_CACHE.move_to_end(key)
                return hit.copy()
    frame = _add_indicators_uncached(df, include_vwap)
    if key is not None:
        with _INDICATOR_LOCK:
            _INDICATOR_CACHE[key] = frame.copy()
            while len(_INDICATOR_CACHE) > _INDICATOR_LIMIT:
                _INDICATOR_CACHE.popitem(last=False)
    return frame


def _add_indicators_uncached(df, include_vwap=False):
    frame = prepare_ohlcv(df)
    if frame.empty:
        return frame
    close = frame["Close"]
    frame["ema20"] = close.ewm(span=20, adjust=False).mean()
    frame["ema50"] = close.ewm(span=50, adjust=False).mean()
    frame["ema200"] = close.ewm(span=200, adjust=False).mean()
    frame["sma50"] = close.rolling(50).mean()
    frame["sma200"] = close.rolling(200).mean()
    frame["bb_mid"] = close.rolling(20).mean()
    deviation = close.rolling(20).std(ddof=0)
    frame["bb_upper"] = frame["bb_mid"] + 2 * deviation
    frame["bb_lower"] = frame["bb_mid"] - 2 * deviation
    delta = close.diff()
    gain = _rma(delta.clip(lower=0), 14)
    loss = _rma((-delta).clip(lower=0), 14)
    relative = gain / loss.replace(0, math.nan)
    frame["rsi"] = 100 - (100 / (1 + relative))
    frame.loc[(loss == 0) & (gain > 0), "rsi"] = 100
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    frame["macd"] = ema12 - ema26
    frame["macd_signal"] = frame["macd"].ewm(span=9, adjust=False).mean()
    frame["macd_hist"] = frame["macd"] - frame["macd_signal"]
    frame["atr"] = _wilder_atr(frame, 14)
    plus_di, minus_di, adx = _dmi(frame, 14)
    frame["plus_di"] = plus_di
    frame["minus_di"] = minus_di
    frame["adx"] = adx
    supertrend, direction = _supertrend(frame)
    frame["supertrend"] = supertrend
    frame["supertrend_dir"] = direction
    average_volume = frame["Volume"].rolling(20).mean()
    frame["volume_avg20"] = average_volume
    frame["rvol"] = frame["Volume"] / average_volume.replace(0, math.nan)
    frame["vwap"] = _session_vwap(frame) if include_vwap else math.nan
    frame["sma5"] = close.rolling(5).mean()
    frame["sma10"] = close.rolling(10).mean()
    frame["sma20"] = close.rolling(20).mean()
    frame["sma100"] = close.rolling(100).mean()
    frame["ema9"] = close.ewm(span=9, adjust=False).mean()
    frame["ema100"] = close.ewm(span=100, adjust=False).mean()
    lowest = frame["Low"].rolling(14).min()
    highest = frame["High"].rolling(14).max()
    span = (highest - lowest).replace(0, math.nan)
    frame["stoch_k"] = 100 * (close - lowest) / span
    frame["stoch_d"] = frame["stoch_k"].rolling(3).mean()
    frame["williams_r"] = -100 * (highest - close) / span
    typical = (frame["High"] + frame["Low"] + close) / 3
    typical_mean = typical.rolling(20).mean()
    mean_dev = _rolling_mean_deviation(typical, 20)
    frame["cci"] = (typical - typical_mean) / (0.015 * mean_dev.replace(0, math.nan))
    frame["roc"] = (close / close.shift(12) - 1) * 100
    frame["momentum"] = close - close.shift(10)
    delta = close.diff()
    signed = delta.gt(0).astype(float) - delta.lt(0).astype(float)
    frame["obv"] = (signed * frame["Volume"]).cumsum()
    frame["donchian_high"] = frame["High"].rolling(20).max()
    frame["donchian_low"] = frame["Low"].rolling(20).min()
    frame["keltner_mid"] = frame["ema20"]
    frame["keltner_upper"] = frame["ema20"] + 2 * frame["atr"]
    frame["keltner_lower"] = frame["ema20"] - 2 * frame["atr"]
    tenkan_high = frame["High"].rolling(9).max()
    tenkan_low = frame["Low"].rolling(9).min()
    kijun_high = frame["High"].rolling(26).max()
    kijun_low = frame["Low"].rolling(26).min()
    frame["ichimoku_tenkan"] = (tenkan_high + tenkan_low) / 2
    frame["ichimoku_kijun"] = (kijun_high + kijun_low) / 2
    span_b = (frame["High"].rolling(52).max() + frame["Low"].rolling(52).min()) / 2
    frame["ichimoku_span_a"] = ((frame["ichimoku_tenkan"] + frame["ichimoku_kijun"]) / 2).shift(26)
    frame["ichimoku_span_b"] = span_b.shift(26)
    psar, psar_dir = _psar(frame)
    frame["psar"] = psar
    frame["psar_dir"] = psar_dir
    frame["golden_cross"] = (frame["sma50"] > frame["sma200"]) & (frame["sma50"].shift(1) <= frame["sma200"].shift(1))
    frame["death_cross"] = (frame["sma50"] < frame["sma200"]) & (frame["sma50"].shift(1) >= frame["sma200"].shift(1))
    return frame


def _candle_time(value):
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is not None:
        stamp = stamp.tz_convert("Asia/Kolkata").tz_localize(None)
    return int(stamp.timestamp())


def _line_points(frame, column):
    if column not in frame.columns:
        return []
    points = []
    for when, value in frame[column].items():
        number = _num(value, 4)
        if number is None:
            continue
        points.append({"time": _candle_time(when), "value": number})
    return points


def _volume_points(frame):
    points = []
    for when, row in frame.iterrows():
        volume = _num(row["Volume"], 0)
        if volume is None:
            continue
        color = "#36e879" if float(row["Close"]) >= float(row["Open"]) else "#ff6161"
        points.append({"time": _candle_time(when), "value": volume, "color": color})
    return points


def _split_supertrend(frame):
    up, down = [], []
    if "supertrend" not in frame.columns:
        return up, down
    for when, row in frame.iterrows():
        value = _num(row["supertrend"], 4)
        direction = _num(row["supertrend_dir"], 0)
        if value is None or direction is None:
            continue
        point = {"time": _candle_time(when), "value": value}
        if direction > 0:
            up.append(point)
        else:
            down.append(point)
    return up, down


def _macd_hist_points(frame):
    points = []
    if "macd_hist" not in frame.columns:
        return points
    for when, value in frame["macd_hist"].items():
        number = _num(value, 4)
        if number is None:
            continue
        points.append({
            "time": _candle_time(when),
            "value": number,
            "color": "#36e879" if number >= 0 else "#ff6161",
        })
    return points


def overlay_series(frame, include_vwap=False):
    """Chart-ready series. Columns that never become finite are omitted."""
    series = {
        "ema20": _line_points(frame, "ema20"),
        "ema50": _line_points(frame, "ema50"),
        "ema200": _line_points(frame, "ema200"),
        "sma50": _line_points(frame, "sma50"),
        "sma200": _line_points(frame, "sma200"),
        "bb_upper": _line_points(frame, "bb_upper"),
        "bb_mid": _line_points(frame, "bb_mid"),
        "bb_lower": _line_points(frame, "bb_lower"),
        "rsi": _line_points(frame, "rsi"),
        "macd": _line_points(frame, "macd"),
        "macd_signal": _line_points(frame, "macd_signal"),
        "macd_hist": _macd_hist_points(frame),
        "volume": _volume_points(frame),
    }
    up, down = _split_supertrend(frame)
    series["supertrend_up"] = up
    series["supertrend_down"] = down
    if include_vwap:
        series["vwap"] = _line_points(frame, "vwap")
    return {name: points for name, points in series.items() if points}


def trend_from_frame(df):
    frame = add_indicators(df, include_vwap=False)
    if frame.empty:
        return {"status": "Unavailable", "detail": "No candles", "basis": "none"}
    price = _num(frame["Close"].iloc[-1], 4)
    ema20 = _num(frame["ema20"].iloc[-1], 4)
    ema50 = _num(frame["ema50"].iloc[-1], 4)
    if price is None:
        return {"status": "Unavailable", "detail": "No close", "basis": "none"}
    if ema20 is not None and ema50 is not None:
        if price > ema20 > ema50:
            return {"status": "Bullish", "detail": "Close above EMA 20 and EMA 50", "basis": "ema"}
        if price < ema20 < ema50:
            return {"status": "Bearish", "detail": "Close below EMA 20 and EMA 50", "basis": "ema"}
        return {"status": "Neutral", "detail": "EMA 20 and EMA 50 are mixed", "basis": "ema"}
    if len(frame) >= 8:
        past = _num(frame["Close"].iloc[-8], 4)
        if past:
            change = (price - past) / past * 100
            if change > 0:
                status = "Bullish"
            elif change < 0:
                status = "Bearish"
            else:
                status = "Neutral"
            return {
                "status": status,
                "detail": f"8-bar change {change:.2f}% (EMA 50 unavailable)",
                "basis": "change",
            }
    return {"status": "Insufficient data", "detail": "Not enough candles for EMA 50", "basis": "none"}


def _zone_distance_pct(zone, price):
    if not zone or price in (None, 0) or zone.get("top") is None or zone.get("bottom") is None:
        return None
    top, bottom = float(zone["top"]), float(zone["bottom"])
    if bottom <= price <= top:
        return 0.0
    if price > top:
        return _num((price - top) / abs(price) * 100)
    return _num((bottom - price) / abs(price) * 100)


def nearest_zones(daily_df, timeframes=("1d", "1wk", "1mo")):
    """Nearest valid zone on each side per timeframe from the shared zone engine."""
    check_superseded()
    return sd_zones.nearest_map(prepare_ohlcv(daily_df), timeframes)


_ZONE_CACHE = {}
_ZONE_CACHE_TTL = 900


def cached_nearest_zones(daily_df, timeframes, cache_key=None, fresh=False):
    """Reuse a zone pass only when the symbol's last daily bar has not changed."""
    key = None if not cache_key else (cache_key, tuple(timeframes))
    now = time.monotonic()
    if key and not fresh:
        hit = _ZONE_CACHE.get(key)
        if hit and now - hit[0] < _ZONE_CACHE_TTL:
            return {name: dict(pair) if isinstance(pair, dict) else pair for name, pair in hit[1].items()}
    found = nearest_zones(daily_df, timeframes)
    if key:
        _ZONE_CACHE[key] = (now, found)
        if len(_ZONE_CACHE) > 400:
            oldest = sorted(_ZONE_CACHE, key=lambda item: _ZONE_CACHE[item][0])[:100]
            for item in oldest:
                _ZONE_CACHE.pop(item, None)
    return {name: dict(pair) if isinstance(pair, dict) else pair for name, pair in found.items()}


def _range_text(zone):
    if not zone or zone.get("bottom") is None or zone.get("top") is None:
        return None
    return f"₹{zone['bottom']:,.2f} – ₹{zone['top']:,.2f}"


def chart_zone_rectangles(daily_df, display_index):
    """Merged nearby zones as chart rectangles plus the "no nearby zone" messages."""
    found = sd_zones.analyze(prepare_ohlcv(daily_df))
    return sd_zones.chart_rectangles(found["display"], display_index), found["messages"], found


def _fib_levels(frame):
    """Retracement prices from the last 120 bars. Empty when the swing is undefined."""
    window = frame.tail(120)
    if len(window) < 30:
        return {}
    high_at = window["High"].idxmax()
    low_at = window["Low"].idxmin()
    high = float(window["High"].max())
    low = float(window["Low"].min())
    span = high - low
    if span <= 0:
        return {}
    try:
        high_pos = int(window.index.get_loc(high_at))
        low_pos = int(window.index.get_loc(low_at))
    except (TypeError, ValueError):
        return {}
    levels = {}
    for ratio in FIB_RATIOS:
        if low_pos > high_pos:
            levels[ratio] = low + ratio * span
        else:
            levels[ratio] = high - ratio * span
    return levels


def fib_level_rows(frame):
    return [
        {"ratio": ratio, "label": f"{ratio * 100:.1f}%", "price": _num(price)}
        for ratio, price in _fib_levels(frame).items()
        if _num(price) is not None
    ]


def _near_level(price, level):
    if price is None or level is None or price == 0:
        return False
    return abs(price - level) / abs(price) <= 0.008


def _parts(row):
    open_price = float(row["Open"])
    high = float(row["High"])
    low = float(row["Low"])
    close = float(row["Close"])
    body = abs(close - open_price)
    span = high - low
    upper = high - max(open_price, close)
    lower = min(open_price, close) - low
    return open_price, high, low, close, body, span, upper, lower


def _long_body(body, span):
    return span > 0 and body >= span * 0.55


def _hammer_shape(row):
    open_price, high, low, close, body, span, upper, lower = _parts(row)
    if span <= 0 or body <= 0:
        return False
    return lower + 1e-9 >= body * 2 and upper <= body + 1e-9 and min(open_price, close) + 1e-9 >= low + span * 0.6


def _inverted_shape(row):
    open_price, high, low, close, body, span, upper, lower = _parts(row)
    if span <= 0 or body <= 0:
        return False
    return upper + 1e-9 >= body * 2 and lower <= body + 1e-9 and max(open_price, close) <= low + span * 0.4 + 1e-9


def _prior_direction(frame):
    """True when the close six bars back is above the close before the signal bar."""
    if len(frame) < 6:
        return None
    earlier = float(frame["Close"].iloc[-6])
    recent = float(frame["Close"].iloc[-2])
    if earlier == recent:
        return None
    return earlier > recent


def _doji(row):
    _, _, _, _, body, span, _, _ = _parts(row)
    return span > 0 and body <= span * 0.1


def _morning_star(frame):
    if len(frame) < 3:
        return None
    first, second, third = frame.iloc[-3], frame.iloc[-2], frame.iloc[-1]
    first_open, _, _, first_close, first_body, first_span, _, _ = _parts(first)
    _, _, _, _, second_body, _, _, _ = _parts(second)
    third_open, _, _, third_close, third_body, third_span, _, _ = _parts(third)
    if first_span <= 0 or third_span <= 0:
        return False
    midpoint = (first_open + first_close) / 2
    return bool(
        first_close < first_open
        and _long_body(first_body, first_span)
        and second_body <= first_body * 0.4
        and third_close > third_open
        and _long_body(third_body, third_span)
        and third_close > midpoint
    )


def _evening_star(frame):
    if len(frame) < 3:
        return None
    first, second, third = frame.iloc[-3], frame.iloc[-2], frame.iloc[-1]
    first_open, _, _, first_close, first_body, first_span, _, _ = _parts(first)
    _, _, _, _, second_body, _, _, _ = _parts(second)
    third_open, _, _, third_close, third_body, third_span, _, _ = _parts(third)
    if first_span <= 0 or third_span <= 0:
        return False
    midpoint = (first_open + first_close) / 2
    return bool(
        first_close > first_open
        and _long_body(first_body, first_span)
        and second_body <= first_body * 0.4
        and third_close < third_open
        and _long_body(third_body, third_span)
        and third_close < midpoint
    )


def _piercing(frame):
    if len(frame) < 2:
        return None
    first, second = frame.iloc[-2], frame.iloc[-1]
    first_open, _, first_low, first_close, first_body, first_span, _, _ = _parts(first)
    second_open, _, _, second_close, _, _, _, _ = _parts(second)
    if first_span <= 0 or not _long_body(first_body, first_span):
        return False
    midpoint = (first_open + first_close) / 2
    return bool(
        first_close < first_open
        and second_close > second_open
        and second_open < first_low
        and midpoint < second_close < first_open
    )


def _dark_cloud(frame):
    if len(frame) < 2:
        return None
    first, second = frame.iloc[-2], frame.iloc[-1]
    first_open, first_high, _, first_close, first_body, first_span, _, _ = _parts(first)
    second_open, _, _, second_close, _, _, _, _ = _parts(second)
    if first_span <= 0 or not _long_body(first_body, first_span):
        return False
    midpoint = (first_open + first_close) / 2
    return bool(
        first_close > first_open
        and second_close < second_open
        and second_open > first_high
        and first_open < second_close < midpoint
    )


def _pivots(series, side, width=3):
    """Positions whose value is the extreme of `width` bars on both sides. Only completed pivots."""
    values = series.tolist()
    found = []
    for index in range(width, len(values) - width):
        window = values[index - width:index + width + 1]
        if side == "low" and values[index] == min(window) and window.count(values[index]) == 1:
            found.append(index)
        if side == "high" and values[index] == max(window) and window.count(values[index]) == 1:
            found.append(index)
    return found


def double_pattern(frame, kind, tolerance=0.03, min_gap=5, min_depth=0.03, lookback=120):
    """Rule-based double bottom or double top on completed bars.

    Two pivots within `tolerance` of each other, at least `min_gap` bars apart,
    separated by a neckline at least `min_depth` away, and the last close beyond
    the neckline. Returns (flag, evidence). flag is None below 30 bars.
    """
    window = frame.tail(lookback)
    if len(window) < 30:
        return None, None
    lows, highs = window["Low"], window["High"]
    closes = window["Close"].tolist()
    if kind == "bottom":
        pivots = _pivots(lows, "low")
    else:
        pivots = _pivots(highs, "high")
    if len(pivots) < 2:
        return False, None
    first, second = pivots[-2], pivots[-1]
    if second - first < min_gap or second < len(window) - 60:
        return False, None
    if kind == "bottom":
        a, b = float(lows.iloc[first]), float(lows.iloc[second])
        neckline = float(highs.iloc[first:second + 1].max())
        floor = min(a, b)
        similar = abs(a - b) / floor <= tolerance
        deep = neckline >= max(a, b) * (1 + min_depth)
        held = all(close >= floor * 0.98 for close in closes[second:])
        confirmed = closes[-1] > neckline
    else:
        a, b = float(highs.iloc[first]), float(highs.iloc[second])
        neckline = float(lows.iloc[first:second + 1].min())
        ceiling = max(a, b)
        similar = abs(a - b) / ceiling <= tolerance
        deep = neckline <= min(a, b) * (1 - min_depth)
        held = all(close <= ceiling * 1.02 for close in closes[second:])
        confirmed = closes[-1] < neckline
    flag = bool(similar and deep and held and confirmed)
    evidence = {
        "first": _candle_time(window.index[first]),
        "second": _candle_time(window.index[second]),
        "first_price": _num(a),
        "second_price": _num(b),
        "neckline": _num(neckline),
    } if flag else None
    return flag, evidence


def _last_patterns(frame, price):
    """Pattern flags. None means the definition could not be evaluated."""
    patterns = {name: None for name in (
        "bullish_candle", "bearish_candle", "bullish_engulfing", "bearish_engulfing",
        "hammer", "inverted_hammer", "shooting_star", "hanging_man", "doji",
        "morning_star", "evening_star", "piercing", "dark_cloud", "inside_bar",
        "breakout_candle", "breakdown_candle", "higher_high_low",
        "double_bottom", "double_top", "fib_golden_zone",
    )}
    fib = {ratio: None for ratio in FIB_RATIOS}
    if len(frame) >= 1:
        last = frame.iloc[-1]
        patterns["bullish_candle"] = float(last["Close"]) > float(last["Open"])
        patterns["bearish_candle"] = float(last["Close"]) < float(last["Open"])
        patterns["doji"] = _doji(last)
    if len(frame) >= 2:
        previous, last = frame.iloc[-2], frame.iloc[-1]
        bullish_body = float(last["Close"]) > float(last["Open"]) and float(previous["Close"]) < float(previous["Open"])
        bearish_body = float(last["Close"]) < float(last["Open"]) and float(previous["Close"]) > float(previous["Open"])
        patterns["bullish_engulfing"] = bool(
            bullish_body
            and float(last["Close"]) >= float(previous["Open"])
            and float(last["Open"]) <= float(previous["Close"])
        )
        patterns["bearish_engulfing"] = bool(
            bearish_body
            and float(last["Close"]) <= float(previous["Open"])
            and float(last["Open"]) >= float(previous["Close"])
        )
        patterns["inside_bar"] = bool(
            float(last["High"]) < float(previous["High"]) and float(last["Low"]) > float(previous["Low"])
        )
        patterns["piercing"] = _piercing(frame)
        patterns["dark_cloud"] = _dark_cloud(frame)
    direction = _prior_direction(frame)
    if len(frame) >= 1 and direction is not None:
        last = frame.iloc[-1]
        hammer = _hammer_shape(last)
        inverted = _inverted_shape(last)
        patterns["hammer"] = bool(hammer and direction)
        patterns["hanging_man"] = bool(hammer and not direction)
        patterns["inverted_hammer"] = bool(inverted and direction)
        patterns["shooting_star"] = bool(inverted and not direction)
    elif len(frame) >= 6:
        patterns["hammer"] = False
        patterns["hanging_man"] = False
        patterns["inverted_hammer"] = False
        patterns["shooting_star"] = False
    if len(frame) >= 3:
        patterns["morning_star"] = _morning_star(frame)
        patterns["evening_star"] = _evening_star(frame)
    if len(frame) >= 21:
        prior = frame.iloc[-21:-1]
        last_close = float(frame["Close"].iloc[-1])
        patterns["breakout_candle"] = last_close > float(prior["High"].max())
        patterns["breakdown_candle"] = last_close < float(prior["Low"].min())
    if len(frame) >= 20:
        recent, prior = frame.tail(10), frame.iloc[-20:-10]
        patterns["higher_high_low"] = bool(
            float(recent["High"].max()) > float(prior["High"].max())
            and float(recent["Low"].min()) > float(prior["Low"].min())
        )
    patterns["double_bottom"], _ = double_pattern(frame, "bottom")
    patterns["double_top"], _ = double_pattern(frame, "top")
    levels = _fib_levels(frame)
    if levels:
        for ratio, level in levels.items():
            fib[ratio] = _near_level(price, level)
        if price is not None:
            low, high = sorted((levels[0.382], levels[0.618]))
            patterns["fib_golden_zone"] = low <= price <= high
    return patterns, fib


def _zone_nearby(zone, price, kind):
    if not zone or price is None or zone.get("top") is None or zone.get("bottom") is None:
        return False
    top, bottom = float(zone["top"]), float(zone["bottom"])
    if bottom <= price <= top:
        return True
    if kind == "demand" and price > top and (price - top) / price <= 0.05:
        return True
    if kind == "supply" and price < bottom and (bottom - price) / price <= 0.05:
        return True
    return False


def _snapshot_values(frame):
    if frame.empty:
        return {}
    row = frame.iloc[-1]
    previous = frame["Close"].iloc[-2] if len(frame) > 1 else None
    price = _num(row["Close"])
    change = None
    if price is not None and _num(previous, 4):
        change = _num((price - float(previous)) / float(previous) * 100)
    return {
        "price": price,
        "change_pct": change,
        "volume": _num(row["Volume"], 0),
        "ema20": _num(row.get("ema20")),
        "ema50": _num(row.get("ema50")),
        "ema200": _num(row.get("ema200")),
        "sma50": _num(row.get("sma50")),
        "sma200": _num(row.get("sma200")),
        "rsi": _num(row.get("rsi")),
        "macd": _num(row.get("macd")),
        "macd_signal": _num(row.get("macd_signal")),
        "macd_hist": _num(row.get("macd_hist")),
        "adx": _num(row.get("adx")),
        "atr": _num(row.get("atr")),
        "volume_avg20": _num(row.get("volume_avg20"), 0),
        "rvol": _num(row.get("rvol")),
        "vwap": _num(row.get("vwap")),
        "bb_upper": _num(row.get("bb_upper")),
        "bb_mid": _num(row.get("bb_mid")),
        "bb_lower": _num(row.get("bb_lower")),
        "supertrend": _num(row.get("supertrend")),
        "supertrend_dir": _num(row.get("supertrend_dir"), 0),
        "sma5": _num(row.get("sma5")),
        "sma20": _num(row.get("sma20")),
        "ema9": _num(row.get("ema9")),
        "stoch_k": _num(row.get("stoch_k")),
        "williams_r": _num(row.get("williams_r")),
        "cci": _num(row.get("cci")),
        "roc": _num(row.get("roc")),
        "momentum": _num(row.get("momentum")),
        "obv": _num(row.get("obv"), 0),
        "plus_di": _num(row.get("plus_di")),
        "minus_di": _num(row.get("minus_di")),
        "psar": _num(row.get("psar")),
        "ichimoku_span_a": _num(row.get("ichimoku_span_a")),
        "ichimoku_span_b": _num(row.get("ichimoku_span_b")),
        "golden_cross": bool(row.get("golden_cross")) if pd.notna(row.get("golden_cross")) else None,
        "death_cross": bool(row.get("death_cross")) if pd.notna(row.get("death_cross")) else None,
    }


def _compare_text(price, level, name):
    if price is None or level is None:
        return "Insufficient data"
    if price > level:
        return f"Price above {name}"
    if price < level:
        return f"Price below {name}"
    return f"Price at {name}"


def technical_rows(values):
    """Panel rows. A missing calculation is reported as insufficient data."""
    price = values.get("price")
    rows = []

    def add(name, value, status):
        rows.append({"name": name, "value": value, "status": status})

    rsi = values.get("rsi")
    if rsi is None:
        add("RSI", None, "Insufficient data")
    elif rsi >= 70:
        add("RSI", rsi, "Overbought")
    elif rsi <= 30:
        add("RSI", rsi, "Oversold")
    elif rsi >= 50:
        add("RSI", rsi, "Bullish, between 50 and 70" if rsi <= 70 else "Bullish")
    else:
        add("RSI", rsi, "Bearish, between 30 and 50")

    macd, signal = values.get("macd"), values.get("macd_signal")
    if macd is None or signal is None:
        add("MACD", macd, "Insufficient data")
    else:
        add("MACD", macd, "Above signal" if macd > signal else "Below signal" if macd < signal else "At signal")

    adx = values.get("adx")
    add("ADX", adx, "Insufficient data" if adx is None else "Strong trend" if adx >= 25 else "Weak trend")
    add("ATR", values.get("atr"), "ATR 14" if values.get("atr") is not None else "Insufficient data")
    add("EMA 20", values.get("ema20"), _compare_text(price, values.get("ema20"), "EMA 20"))
    add("EMA 50", values.get("ema50"), _compare_text(price, values.get("ema50"), "EMA 50"))
    add("EMA 200", values.get("ema200"), _compare_text(price, values.get("ema200"), "EMA 200"))
    add("SMA 50", values.get("sma50"), _compare_text(price, values.get("sma50"), "SMA 50"))
    add("SMA 200", values.get("sma200"), _compare_text(price, values.get("sma200"), "SMA 200"))

    volume, average = values.get("volume"), values.get("volume_avg20")
    if volume is None:
        add("Volume", None, "Insufficient data")
    elif average is None:
        add("Volume", volume, "20-bar average unavailable")
    elif volume > average:
        add("Volume", volume, "Above 20-bar average")
    elif volume < average:
        add("Volume", volume, "Below 20-bar average")
    else:
        add("Volume", volume, "At 20-bar average")

    rvol = values.get("rvol")
    if rvol is None:
        add("Relative Volume", None, "Insufficient data")
    elif rvol >= 1.5:
        add("Relative Volume", rvol, "Above 1.5×")
    elif rvol >= 1:
        add("Relative Volume", rvol, "Above average")
    else:
        add("Relative Volume", rvol, "Below average")

    vwap = values.get("vwap")
    if vwap is None:
        add("VWAP", None, "Session VWAP unavailable on this timeframe")
    else:
        basis = "Session VWAP" if values.get("vwap_basis") == "session" else "VWAP"
        add("VWAP", vwap, _compare_text(price, vwap, basis))

    upper, lower = values.get("bb_upper"), values.get("bb_lower")
    if price is None or upper is None or lower is None:
        add("Bollinger Bands", None, "Insufficient data")
    elif price > upper:
        add("Bollinger Bands", upper, "Close above upper band")
    elif price < lower:
        add("Bollinger Bands", lower, "Close below lower band")
    else:
        add("Bollinger Bands", values.get("bb_mid"), "Close inside bands")

    direction = values.get("supertrend_dir")
    line = values.get("supertrend")
    if direction is None or line is None:
        add("Supertrend", None, "Insufficient data")
    else:
        add("Supertrend", line, "Bullish" if direction > 0 else "Bearish")
    add("Stochastic %K", values.get("stoch_k"), "14,3" if values.get("stoch_k") is not None else "Insufficient data")
    add("Williams %R", values.get("williams_r"), "14" if values.get("williams_r") is not None else "Insufficient data")
    add("CCI", values.get("cci"), "20" if values.get("cci") is not None else "Insufficient data")
    add("OBV", values.get("obv"), "Cumulative" if values.get("obv") is not None else "Insufficient data")
    add("+DI", values.get("plus_di"), "14" if values.get("plus_di") is not None else "Insufficient data")
    add("-DI", values.get("minus_di"), "14" if values.get("minus_di") is not None else "Insufficient data")
    add("Parabolic SAR", values.get("psar"), "0.02 / 0.20" if values.get("psar") is not None else "Insufficient data")
    span_a, span_b = values.get("ichimoku_span_a"), values.get("ichimoku_span_b")
    if price is None or span_a is None or span_b is None:
        add("Ichimoku", None, "Insufficient data")
    elif price > max(span_a, span_b):
        add("Ichimoku", span_a, "Close above cloud")
    elif price < min(span_a, span_b):
        add("Ichimoku", span_b, "Close below cloud")
    else:
        add("Ichimoku", span_a, "Close inside cloud")
    return rows


def _ema_bullish(trend):
    return trend.get("basis") == "ema" and trend.get("status") == "Bullish"


def _ema_bearish(trend):
    return trend.get("basis") == "ema" and trend.get("status") == "Bearish"


ZONE_CONDITION_IDS = {
    "daily_demand", "weekly_demand", "monthly_demand",
    "daily_supply", "weekly_supply", "monthly_supply",
}
VWAP_CONDITION_IDS = {"price_above_vwap", "price_below_vwap", "vwap_bullish", "vwap_bearish"}
EMPTY_ZONES = {
    "1d": {"demand": None, "supply": None},
    "1wk": {"demand": None, "supply": None},
    "1mo": {"demand": None, "supply": None},
}


def ema_alignment(values):
    ema20, ema50, ema200 = values.get("ema20"), values.get("ema50"), values.get("ema200")
    if ema20 is None or ema50 is None or ema200 is None:
        return "Unavailable"
    if ema20 > ema50 > ema200:
        return "Bullish"
    if ema20 < ema50 < ema200:
        return "Bearish"
    return "Mixed"


def _vwap_slope(frame):
    if frame is None or frame.empty or "vwap" not in frame.columns or len(frame) < 6:
        return None
    last = _num(frame["vwap"].iloc[-1], 4)
    earlier = _num(frame["vwap"].iloc[-6], 4)
    if last is None or earlier is None:
        return None
    return last - earlier


def build_context(scan_df, daily_df=None, include_zones=True, zone_timeframes=None, include_vwap=False, params=None):
    """Scanner context. Indicator values come from scan_df. Daily zones stay on daily bars."""
    scan = add_indicators(prepare_ohlcv(scan_df), include_vwap=include_vwap)
    if scan.empty or _num(scan["Close"].iloc[-1]) is None:
        return None
    values = _snapshot_values(scan)
    if values.get("atr") is not None and values.get("price"):
        values["atr_pct"] = _num(values["atr"] / values["price"] * 100)
    else:
        values["atr_pct"] = None
    values["vwap_slope"] = _vwap_slope(scan) if include_vwap else None
    if include_vwap and values.get("vwap") is not None:
        values["vwap_basis"] = "session"
    previous = _snapshot_values(scan.iloc[:-1]) if len(scan) > 1 else {}
    daily_raw = daily_df if daily_df is not None else scan_df
    daily = add_indicators(prepare_ohlcv(daily_raw), include_vwap=False)
    if daily.empty:
        missing = {"status": "Insufficient data", "detail": "No daily candles", "basis": "none"}
        daily_trend = weekly = monthly = missing
        zones = {key: {"demand": None, "supply": None} for key in ("1d", "1wk", "1mo")}
        zones_calculated = False
    else:
        daily_trend = trend_from_frame(daily)
        weekly = trend_from_frame(resample_chart(daily, "1wk"))
        monthly = trend_from_frame(resample_chart(daily, "1mo"))
        if include_zones:
            zones = nearest_zones(daily, zone_timeframes or ("1d", "1wk", "1mo"))
            zones_calculated = True
        else:
            zones = {key: {"demand": None, "supply": None} for key in ("1d", "1wk", "1mo")}
            zones_calculated = False
    # 52-week high needs close to a full year of daily bars; 240 allows for holidays.
    values["high_52w"] = _num(daily["High"].tail(252).max()) if len(daily) >= 240 else None
    patterns, fib = _last_patterns(scan, values.get("price"))
    return {
        "params": clean_scan_params(params),
        "values": values,
        "previous": previous,
        "trends": {"1d": daily_trend, "1wk": weekly, "1mo": monthly, "scan": trend_from_frame(scan)},
        "zones": zones,
        "zones_calculated": zones_calculated,
        "patterns": patterns,
        "fib": fib,
        "vwap_enabled": include_vwap,
        "frame": scan,
    }


def _trend_state(trend, wanted):
    if not trend or trend.get("basis") != "ema":
        return "unavailable"
    return "match" if trend.get("status") == wanted else "fail"


def _ready(ok, available):
    if not available:
        return "unavailable"
    return "match" if ok else "fail"


def condition_state(condition_id, context):
    """match, fail, or unavailable. Unavailable is never reported as a match."""
    values = context["values"]
    trends = context["trends"]
    zones = context["zones"]
    patterns = context["patterns"]
    fib = context["fib"]
    price = values.get("price")
    params = context.get("params") or SCAN_PARAM_DEFAULTS
    rsi, adx, rvol, atr_pct = values.get("rsi"), values.get("adx"), values.get("rvol"), values.get("atr_pct")
    rsi_min, rsi_max = params.get("rsi_min"), params.get("rsi_max")
    vol_mult = params.get("vol_mult")
    high_52w, near_pct = values.get("high_52w"), params.get("near_52w_pct")
    price_min, price_max = params.get("price_min"), params.get("price_max")
    range_ready = price is not None and (price_min is not None or price_max is not None)

    def pattern_state(key):
        value = patterns.get(key, None)
        if value is None:
            return "unavailable"
        return "match" if value else "fail"

    def fib_state(ratio):
        value = fib.get(ratio, None)
        if value is None:
            return "unavailable"
        return "match" if value else "fail"

    def zone_state(timeframe, kind):
        if not context.get("zones_calculated"):
            return "unavailable"
        zone = zones.get(timeframe, {}).get(kind)
        return "match" if _zone_nearby(zone, price, kind) else "fail"

    vwap = values.get("vwap")
    slope = values.get("vwap_slope")
    vwap_ready = context.get("vwap_enabled") and vwap is not None and price is not None
    slope_ready = vwap_ready and slope is not None
    checks = {
        "daily_bullish": _trend_state(trends.get("1d"), "Bullish"),
        "daily_bearish": _trend_state(trends.get("1d"), "Bearish"),
        "weekly_bullish": _trend_state(trends.get("1wk"), "Bullish"),
        "weekly_bearish": _trend_state(trends.get("1wk"), "Bearish"),
        "monthly_bullish": _trend_state(trends.get("1mo"), "Bullish"),
        "monthly_bearish": _trend_state(trends.get("1mo"), "Bearish"),
        "price_above_ema20": _ready(_above(price, values.get("ema20")), price is not None and values.get("ema20") is not None),
        "price_above_ema50": _ready(_above(price, values.get("ema50")), price is not None and values.get("ema50") is not None),
        "price_above_ema200": _ready(_above(price, values.get("ema200")), price is not None and values.get("ema200") is not None),
        "price_below_ema20": _ready(_below(price, values.get("ema20")), price is not None and values.get("ema20") is not None),
        "ema20_gt_ema50": _ready(_above(values.get("ema20"), values.get("ema50")), values.get("ema20") is not None and values.get("ema50") is not None),
        "ema50_gt_ema200": _ready(_above(values.get("ema50"), values.get("ema200")), values.get("ema50") is not None and values.get("ema200") is not None),
        "price_above_sma50": _ready(_above(price, values.get("sma50")), price is not None and values.get("sma50") is not None),
        "price_above_sma200": _ready(_above(price, values.get("sma200")), price is not None and values.get("sma200") is not None),
        "sma50_gt_sma200": _ready(_above(values.get("sma50"), values.get("sma200")), values.get("sma50") is not None and values.get("sma200") is not None),
        "golden_cross": _ready(bool(values.get("golden_cross")), values.get("sma50") is not None and values.get("sma200") is not None),
        "death_cross": _ready(bool(values.get("death_cross")), values.get("sma50") is not None and values.get("sma200") is not None),
        "rsi_above_50": _ready(values.get("rsi") is not None and values["rsi"] > 50, values.get("rsi") is not None),
        "rsi_50_70": _ready(
            rsi is not None and rsi_min is not None and rsi_max is not None and rsi_min <= rsi <= rsi_max,
            rsi is not None and rsi_min is not None and rsi_max is not None,
        ),
        "rsi_oversold": _ready(values.get("rsi") is not None and values["rsi"] < 30, values.get("rsi") is not None),
        "rsi_overbought": _ready(values.get("rsi") is not None and values["rsi"] > 70, values.get("rsi") is not None),
        "macd_above_signal": _ready(_above(values.get("macd"), values.get("macd_signal")), values.get("macd") is not None and values.get("macd_signal") is not None),
        "macd_hist_positive": _ready(values.get("macd_hist") is not None and values["macd_hist"] > 0, values.get("macd_hist") is not None),
        "volume_gt_avg": _ready(
            values.get("volume") is not None and values.get("volume_avg20") is not None and vol_mult is not None
            and values["volume"] > vol_mult * values["volume_avg20"],
            values.get("volume") is not None and values.get("volume_avg20") is not None and vol_mult is not None,
        ),
        "rvol_gt_1_5": _ready(rvol is not None and params.get("rvol_min") is not None and rvol > params["rvol_min"], rvol is not None and params.get("rvol_min") is not None),
        "atr_pct_gt": _ready(atr_pct is not None and params.get("atr_pct") is not None and atr_pct > params["atr_pct"], atr_pct is not None and params.get("atr_pct") is not None),
        "near_52w_high": _ready(
            price is not None and high_52w and near_pct is not None and price >= high_52w * (1 - near_pct / 100),
            price is not None and bool(high_52w) and near_pct is not None,
        ),
        "price_range": _ready(
            range_ready and (price_min is None or price >= price_min) and (price_max is None or price <= price_max),
            range_ready,
        ),
        "volume_gt_2x": _ready(
            values.get("volume") is not None and values.get("volume_avg20") not in (None, 0) and values["volume"] > 2 * values["volume_avg20"],
            values.get("volume") is not None and values.get("volume_avg20") not in (None, 0),
        ),
        "price_above_vwap": _ready(vwap_ready and price > vwap, vwap_ready),
        "price_below_vwap": _ready(vwap_ready and price < vwap, vwap_ready),
        "vwap_bullish": _ready(slope_ready and price > vwap and slope > 0, slope_ready),
        "vwap_bearish": _ready(slope_ready and price < vwap and slope < 0, slope_ready),
        "above_upper_bb": _ready(_above(price, values.get("bb_upper")), price is not None and values.get("bb_upper") is not None),
        "below_lower_bb": _ready(_below(price, values.get("bb_lower")), price is not None and values.get("bb_lower") is not None),
        "inside_bb": _ready(_inside(price, values.get("bb_lower"), values.get("bb_upper")), price is not None and values.get("bb_lower") is not None and values.get("bb_upper") is not None),
        "adx_gt_25": _ready(adx is not None and params.get("adx_min") is not None and adx > params["adx_min"], adx is not None and params.get("adx_min") is not None),
        "supertrend_bullish": _ready(values.get("supertrend_dir") is not None and values["supertrend_dir"] > 0, values.get("supertrend_dir") is not None),
        "supertrend_bearish": _ready(values.get("supertrend_dir") is not None and values["supertrend_dir"] < 0, values.get("supertrend_dir") is not None),
        "price_above_cloud": _ready(
            price is not None and values.get("ichimoku_span_a") is not None and values.get("ichimoku_span_b") is not None and price > max(values["ichimoku_span_a"], values["ichimoku_span_b"]),
            price is not None and values.get("ichimoku_span_a") is not None and values.get("ichimoku_span_b") is not None,
        ),
        "price_below_cloud": _ready(
            price is not None and values.get("ichimoku_span_a") is not None and values.get("ichimoku_span_b") is not None and price < min(values["ichimoku_span_a"], values["ichimoku_span_b"]),
            price is not None and values.get("ichimoku_span_a") is not None and values.get("ichimoku_span_b") is not None,
        ),
        "bullish_candle": pattern_state("bullish_candle"),
        "bearish_candle": pattern_state("bearish_candle"),
        "bullish_engulfing": pattern_state("bullish_engulfing"),
        "bearish_engulfing": pattern_state("bearish_engulfing"),
        "hammer": pattern_state("hammer"),
        "inverted_hammer": pattern_state("inverted_hammer"),
        "shooting_star": pattern_state("shooting_star"),
        "hanging_man": pattern_state("hanging_man"),
        "doji": pattern_state("doji"),
        "morning_star": pattern_state("morning_star"),
        "evening_star": pattern_state("evening_star"),
        "piercing": pattern_state("piercing"),
        "dark_cloud": pattern_state("dark_cloud"),
        "inside_bar": pattern_state("inside_bar"),
        "breakout_candle": pattern_state("breakout_candle"),
        "breakdown_candle": pattern_state("breakdown_candle"),
        "higher_high_low": pattern_state("higher_high_low"),
        "double_bottom": pattern_state("double_bottom"),
        "double_top": pattern_state("double_top"),
        "fib_golden_zone": pattern_state("fib_golden_zone"),
        "near_fib_236": fib_state(0.236),
        "near_fib_382": fib_state(0.382),
        "near_fib_500": fib_state(0.5),
        "near_fib_618": fib_state(0.618),
        "near_fib_786": fib_state(0.786),
        "daily_demand": zone_state("1d", "demand"),
        "weekly_demand": zone_state("1wk", "demand"),
        "monthly_demand": zone_state("1mo", "demand"),
        "daily_supply": zone_state("1d", "supply"),
        "weekly_supply": zone_state("1wk", "supply"),
        "monthly_supply": zone_state("1mo", "supply"),
        "mtf_daily_weekly_bullish": _ready(
            _trend_state(trends.get("1d"), "Bullish") == "match" and _trend_state(trends.get("1wk"), "Bullish") == "match",
            _trend_state(trends.get("1d"), "Bullish") != "unavailable" and _trend_state(trends.get("1wk"), "Bullish") != "unavailable",
        ),
        "mtf_all_bullish": _ready(
            all(_trend_state(trends.get(key), "Bullish") == "match" for key in ("1d", "1wk", "1mo")),
            all(_trend_state(trends.get(key), "Bullish") != "unavailable" for key in ("1d", "1wk", "1mo")),
        ),
        "change_positive": _ready(values.get("change_pct") is not None and values["change_pct"] > 0, values.get("change_pct") is not None),
        "change_negative": _ready(values.get("change_pct") is not None and values["change_pct"] < 0, values.get("change_pct") is not None),
    }
    return checks.get(condition_id, "unavailable")


def condition_met(condition_id, context):
    return condition_state(condition_id, context) == "match"


def _above(left, right):
    return left is not None and right is not None and left > right


def _below(left, right):
    return left is not None and right is not None and left < right


def _inside(price, lower, upper):
    return price is not None and lower is not None and upper is not None and lower <= price <= upper


def _fmt_param(value):
    return "any" if value is None else f"{value:g}"


def condition_label(condition_id, params=None):
    """Catalog label with the threshold actually used, so the report matches the calculation."""
    params = params or SCAN_PARAM_DEFAULTS
    texts = {
        "rsi_50_70": lambda: f"RSI {_fmt_param(params.get('rsi_min'))}–{_fmt_param(params.get('rsi_max'))}",
        "adx_gt_25": lambda: f"ADX above {_fmt_param(params.get('adx_min'))}",
        "volume_gt_avg": lambda: f"Volume above {_fmt_param(params.get('vol_mult'))}x the 20-bar average",
        "rvol_gt_1_5": lambda: f"Relative volume above {_fmt_param(params.get('rvol_min'))}x",
        "atr_pct_gt": lambda: f"ATR 14 above {_fmt_param(params.get('atr_pct'))}% of close",
        "near_52w_high": lambda: f"Within {_fmt_param(params.get('near_52w_pct'))}% of the 52-week high",
        "price_range": lambda: f"Close between {_fmt_param(params.get('price_min'))} and {_fmt_param(params.get('price_max'))}",
    }
    return texts[condition_id]() if condition_id in texts else CONDITION_LABELS.get(condition_id, condition_id)


def condition_report(condition_ids, context):
    matched, failed, unavailable = [], [], []
    for item in condition_ids:
        state = condition_state(item, context)
        label = condition_label(item, context.get("params"))
        if state == "match":
            matched.append(label)
        elif state == "fail":
            failed.append(label)
        else:
            unavailable.append(label)
    evaluated = len(matched) + len(failed)
    score = None if evaluated == 0 else int(round(100 * len(matched) / evaluated))
    formula = (
        f"{len(matched)} matched / {evaluated} calculated"
        + (f" = {score}" if score is not None else "")
        + f". {len(unavailable)} unavailable excluded from the score."
    )
    return {
        "score": score,
        "matched": matched,
        "failed": failed,
        "unavailable": unavailable,
        "matched_count": len(matched),
        "enabled_count": len(condition_ids),
        "formula": formula,
    }


def matches_logic(condition_ids, context, logic):
    if not condition_ids:
        return False
    states = [condition_state(item, context) for item in condition_ids]
    if logic == "OR":
        return any(state == "match" for state in states)
    return all(state == "match" for state in states)


def _macd_text(values):
    if values.get("macd") is None or values.get("macd_signal") is None:
        return "Unavailable"
    return "Above signal" if values["macd"] > values["macd_signal"] else "Below signal"


def _supertrend_text(values):
    direction = values.get("supertrend_dir")
    if direction is None:
        return "Unavailable"
    return "Bullish" if direction > 0 else "Bearish"


def _metrics(frame, include_vwap=False):
    prepared = prepare_ohlcv(frame) if frame is not None else pd.DataFrame()
    if prepared.empty or len(prepared) < 20:
        return None
    enriched = add_indicators(prepared, include_vwap=include_vwap)
    return _snapshot_values(enriched), trend_from_frame(enriched)


def mtf_board(daily_df, intraday_frames=None, zone_map=None, intraday_zones=None):
    """One row per timeframe. Missing candles stay unavailable and are not copied from daily."""
    intraday_frames = intraday_frames or {}
    zone_map = zone_map or EMPTY_ZONES
    intraday_zones = intraday_zones or {}
    specs = (
        ("5m", "5M", "intraday"),
        ("15m", "15M", "intraday"),
        ("1h", "1H", "intraday"),
        ("4h", "4H", "intraday"),
        ("1d", "1D", "1d"),
        ("1wk", "1W", "1wk"),
        ("1mo", "1M", "1mo"),
    )
    rows = []
    for key, label, kind in specs:
        if kind == "intraday":
            frame = intraday_frames.get(key)
            if frame is None or getattr(frame, "empty", True):
                rows.append({
                    "timeframe": key, "label": label, "status": "Unavailable",
                    "detail": "No intraday candles from the data provider",
                    "ema": "Unavailable", "rsi": None, "macd": "Unavailable", "adx": None,
                    "supertrend": "Unavailable", "demand": None, "supply": None,
                    "demand_state": "unavailable", "supply_state": "unavailable",
                })
                continue
            metrics = _metrics(frame, include_vwap=True)
            demand = (intraday_zones.get(key) or {}).get("demand")
            supply = (intraday_zones.get(key) or {}).get("supply")
            if key not in intraday_zones:
                demand_state = supply_state = "unavailable"
            else:
                demand_state = "calculated" if demand else "none"
                supply_state = "calculated" if supply else "none"
        else:
            source = daily_df if kind == "1d" else resample_chart(daily_df, kind)
            metrics = _metrics(source, include_vwap=False)
            demand = zone_map.get(kind, {}).get("demand")
            supply = zone_map.get(kind, {}).get("supply")
            calculated = zone_map.get("_calculated")
            if calculated is not None and kind not in calculated:
                demand_state = supply_state = "unavailable"
            else:
                demand_state = "calculated" if demand else "none"
                supply_state = "calculated" if supply else "none"
        if not metrics:
            rows.append({
                "timeframe": key, "label": label, "status": "Unavailable",
                "detail": "Not enough candles",
                "ema": "Unavailable", "rsi": None, "macd": "Unavailable", "adx": None,
                "supertrend": "Unavailable", "demand": None, "supply": None,
                "demand_state": "unavailable", "supply_state": "unavailable",
            })
            continue
        values, trend = metrics
        rows.append({
            "timeframe": key,
            "label": label,
            "status": trend["status"],
            "detail": trend["detail"],
            "ema": ema_alignment(values),
            "rsi": values.get("rsi"),
            "macd": _macd_text(values),
            "adx": values.get("adx"),
            "supertrend": _supertrend_text(values),
            "demand": _range_text(demand),
            "supply": _range_text(supply),
            "demand_state": demand_state,
            "supply_state": supply_state,
        })
    return rows


def mtf_status_text(rows):
    return " · ".join(f"{row['label']} {row['status']}" for row in rows)


def _nearest_distance(demand, supply):
    distances = [
        zone.get("distance_pct")
        for zone in (demand, supply)
        if zone and zone.get("distance_pct") is not None
    ]
    return min(distances) if distances else None


def scan_symbol(scan_df, symbol, company, condition_ids, logic, daily_df=None, intraday_frames=None, timeframe="1d", progress=None, fresh=False, params=None):
    """Return one result row when the selected timeframe matches, otherwise None.

    A missing intraday frame is unavailable. Daily candles are not substituted.
    """
    if timeframe in INTRADAY_SCAN and (scan_df is None or getattr(scan_df, "empty", True)):
        return "unavailable"
    include_vwap = timeframe in INTRADAY_SCAN
    if progress:
        progress("Technical calculation")
    context = build_context(scan_df, daily_df=daily_df, include_zones=False, include_vwap=include_vwap, params=params)
    if context is None:
        return "unavailable"
    if progress:
        progress("Scanner conditions")
    zone_ids = [item for item in condition_ids if item in ZONE_CONDITION_IDS]
    other_ids = [item for item in condition_ids if item not in ZONE_CONDITION_IDS]
    other_states = [condition_state(item, context) for item in other_ids]
    if logic == "AND" and other_ids and any(state != "match" for state in other_states):
        return None
    if logic == "OR" and not zone_ids and not any(state == "match" for state in other_states):
        return None
    zone_timeframes = ["1d"]
    if any(item.startswith("weekly_") for item in zone_ids):
        zone_timeframes.append("1wk")
    if any(item.startswith("monthly_") for item in zone_ids):
        zone_timeframes.append("1mo")
    daily_source = daily_df if daily_df is not None else scan_df
    if progress:
        progress("Demand and supply")
    source = prepare_ohlcv(daily_source)
    last_bar = "" if source.empty else str(source.index[-1])
    context["zones"] = cached_nearest_zones(daily_source, zone_timeframes, (symbol, last_bar), fresh=fresh)
    context["zones"]["_calculated"] = list(zone_timeframes)
    context["zones_calculated"] = True
    if progress:
        progress("Score")
    if not matches_logic(condition_ids, context, logic):
        return None
    report = condition_report(condition_ids, context)
    values = context["values"]
    daily_zones = context["zones"]["1d"]
    demand = daily_zones["demand"]
    supply = daily_zones["supply"]
    active_patterns = [CONDITION_LABELS[key] for key, flag in context["patterns"].items() if flag is True and key in CONDITION_LABELS]
    board = mtf_board(daily_source, intraday_frames, context["zones"])
    from services.signal_engine import build_signal_from_enriched
    built = build_signal_from_enriched(
        context["frame"],
        include_vwap=include_vwap,
        zones=daily_zones,
        zones_calculated=True,
    )
    return {
        "symbol": symbol,
        "company": company,
        "ltp": values.get("price"),
        "change_pct": values.get("change_pct"),
        "volume": values.get("volume"),
        "rsi": values.get("rsi"),
        "macd": values.get("macd"),
        "adx": values.get("adx"),
        "trend": context["trends"]["scan"]["status"],
        "trend_detail": context["trends"]["scan"]["detail"],
        "timeframe": timeframe,
        "ema_alignment": ema_alignment(values),
        "ema20_vs_ema50": (
            "unavailable" if values.get("ema20") is None or values.get("ema50") is None
            else "above" if values["ema20"] > values["ema50"]
            else "below" if values["ema20"] < values["ema50"]
            else "equal"
        ),
        "vwap": values.get("vwap"),
        "vwap_state": "calculated" if values.get("vwap") is not None else "unavailable",
        "rvol": values.get("rvol"),
        "score": report["score"],
        "score_formula": report["formula"],
        "matched": report["matched"],
        "failed": report["failed"],
        "unavailable_conditions": report["unavailable"],
        "matched_count": report["matched_count"],
        "enabled_count": report["enabled_count"],
        "demand_zone": _range_text(demand),
        "supply_zone": _range_text(supply),
        "demand_distance_pct": None if not demand else demand.get("distance_pct"),
        "supply_distance_pct": None if not supply else supply.get("distance_pct"),
        "nearest_zone_distance_pct": _nearest_distance(demand, supply),
        "demand_status": None if not demand else demand.get("status"),
        "supply_status": None if not supply else supply.get("status"),
        "pattern": " · ".join(active_patterns[:3]) if active_patterns else None,
        "mtf": board,
        "mtf_status": mtf_status_text(board),
        "zone_map": context["zones"],
        "remarks": report["formula"],
        "signal": None if built.get("data_unavailable") else built.get("signal"),
        "signal_label": built.get("label"),
        "signal_score": built.get("strength"),
        "entry": built.get("entry"),
        "stop_loss": built.get("stop_loss"),
        "target_1": built.get("target_1"),
        "target_2": built.get("target_2"),
        "target_3": (built.get("levels") or {}).get("target_3"),
        "risk_reward": built.get("risk_reward"),
        "reasons": built.get("reasons") or [],
    }


def evaluate_alert(rule, context):
    """In-app alert check. This does not send email, push, or a broker order."""
    kind = rule.get("type")
    values = context["values"]
    previous = context.get("previous") or {}
    price = values.get("price")
    if kind == "demand_entry":
        zone = context["zones"]["1d"]["demand"]
        triggered = bool(zone and price is not None and float(zone["bottom"]) <= price <= float(zone["top"]))
        return {"triggered": triggered, "state": "match" if triggered else "fail", "detail": "Price is inside daily demand" if triggered else "Price is outside daily demand"}
    if kind == "supply_entry":
        zone = context["zones"]["1d"]["supply"]
        triggered = bool(zone and price is not None and float(zone["bottom"]) <= price <= float(zone["top"]))
        return {"triggered": triggered, "state": "match" if triggered else "fail", "detail": "Price is inside daily supply" if triggered else "Price is outside daily supply"}
    if kind == "ema_cross":
        ready = all(values.get(key) is not None and previous.get(key) is not None for key in ("ema20", "ema50"))
        if not ready:
            return {"triggered": False, "state": "unavailable", "detail": "EMA cross needs two calculated bars"}
        direction = rule.get("direction", "bullish")
        if direction == "bearish":
            triggered = previous["ema20"] >= previous["ema50"] and values["ema20"] < values["ema50"]
            detail = "EMA 20 crossed below EMA 50" if triggered else "No bearish EMA cross on the last bar"
        else:
            triggered = previous["ema20"] <= previous["ema50"] and values["ema20"] > values["ema50"]
            detail = "EMA 20 crossed above EMA 50" if triggered else "No bullish EMA cross on the last bar"
        return {"triggered": triggered, "state": "match" if triggered else "fail", "detail": detail}
    if kind == "rsi_threshold":
        rsi = values.get("rsi")
        level = rule.get("value")
        if rsi is None or not isinstance(level, (int, float)):
            return {"triggered": False, "state": "unavailable", "detail": "RSI threshold needs a calculated RSI and a numeric level"}
        operator = rule.get("operator", "below")
        triggered = rsi <= level if operator == "below" else rsi >= level
        return {"triggered": triggered, "state": "match" if triggered else "fail", "detail": f"RSI {rsi} {'<=' if operator == 'below' else '>='} {level}"}
    if kind == "macd_cross":
        ready = all(values.get(key) is not None and previous.get(key) is not None for key in ("macd", "macd_signal"))
        if not ready:
            return {"triggered": False, "state": "unavailable", "detail": "MACD cross needs two calculated bars"}
        direction = rule.get("direction", "bullish")
        if direction == "bearish":
            triggered = previous["macd"] >= previous["macd_signal"] and values["macd"] < values["macd_signal"]
        else:
            triggered = previous["macd"] <= previous["macd_signal"] and values["macd"] > values["macd_signal"]
        return {"triggered": triggered, "state": "match" if triggered else "fail", "detail": "MACD crossed its signal" if triggered else "No MACD cross on the last bar"}
    if kind == "supertrend_change":
        current = values.get("supertrend_dir")
        prior = previous.get("supertrend_dir")
        if current is None or prior is None:
            return {"triggered": False, "state": "unavailable", "detail": "Supertrend change needs two calculated bars"}
        triggered = current != prior
        return {"triggered": triggered, "state": "match" if triggered else "fail", "detail": "Supertrend direction changed" if triggered else "Supertrend direction unchanged"}
    if kind == "vwap_cross":
        if not context.get("vwap_enabled") or values.get("vwap") is None or previous.get("vwap") is None or price is None or previous.get("price") is None:
            return {"triggered": False, "state": "unavailable", "detail": "VWAP cross requires intraday session VWAP"}
        crossed_up = previous["price"] <= previous["vwap"] and price > values["vwap"]
        crossed_down = previous["price"] >= previous["vwap"] and price < values["vwap"]
        direction = rule.get("direction", "bullish")
        triggered = crossed_up if direction != "bearish" else crossed_down
        return {"triggered": triggered, "state": "match" if triggered else "fail", "detail": "Price crossed session VWAP" if triggered else "No VWAP cross on the last bar"}
    if kind == "score_threshold":
        selected = [item for item in rule.get("conditions", []) if item in CONDITION_LABELS]
        minimum = rule.get("value")
        if not selected or not isinstance(minimum, (int, float)):
            return {"triggered": False, "state": "unavailable", "detail": "Score alert needs conditions and a numeric threshold"}
        report = condition_report(selected, context)
        if report["score"] is None:
            return {"triggered": False, "state": "unavailable", "detail": report["formula"]}
        triggered = report["score"] >= minimum
        return {"triggered": triggered, "state": "match" if triggered else "fail", "detail": report["formula"]}
    if kind in {"buy_signal", "sell_signal", "breakout", "breakdown", "volume_breakout", "target_reached", "stop_reached"}:
        return _extended_alert(kind, rule, context)
    return {"triggered": False, "state": "unavailable", "detail": "Unknown alert type"}


def _extended_alert(kind, rule, context):
    """Extra in-app checks. Nothing is sent to a broker."""
    values = context["values"]
    frame = context.get("frame")
    if kind in {"buy_signal", "sell_signal"}:
        if frame is None or getattr(frame, "empty", True):
            return {"triggered": False, "state": "unavailable", "detail": "No candles for the signal alert"}
        from services.signal_engine import build_signal_from_enriched
        zones = (context.get("zones") or {}).get("1d")
        signal = build_signal_from_enriched(
            frame,
            include_vwap=bool(context.get("vwap_enabled")),
            zones=zones,
            zones_calculated=bool(context.get("zones_calculated")),
        )
        wanted = "BUY" if kind == "buy_signal" else "SELL"
        triggered = signal.get("signal") == wanted
        detail = signal.get("label") or "Signal unavailable"
        if signal.get("data_unavailable"):
            return {"triggered": False, "state": "unavailable", "detail": "DATA UNAVAILABLE"}
        return {"triggered": triggered, "state": "match" if triggered else "fail", "detail": detail}
    if kind == "breakout":
        triggered = context.get("patterns", {}).get("breakout_candle") is True
        return {"triggered": triggered, "state": "match" if triggered else "fail", "detail": "Close is above the prior 20-bar high" if triggered else "No breakout candle"}
    if kind == "breakdown":
        triggered = context.get("patterns", {}).get("breakdown_candle") is True
        return {"triggered": triggered, "state": "match" if triggered else "fail", "detail": "Close is below the prior 20-bar low" if triggered else "No breakdown candle"}
    if kind == "volume_breakout":
        rvol = values.get("rvol")
        change = values.get("change_pct")
        if rvol is None or change is None:
            return {"triggered": False, "state": "unavailable", "detail": "Relative volume or the last bar change is missing"}
        triggered = rvol > 1.5 and change > 0 and context.get("patterns", {}).get("breakout_candle") is True
        return {"triggered": triggered, "state": "match" if triggered else "fail", "detail": f"Relative volume {rvol} on a breakout bar" if triggered else "Volume breakout conditions are not all true"}
    high = low = None
    if frame is not None and not getattr(frame, "empty", True):
        high = _num(frame["High"].iloc[-1], 4)
        low = _num(frame["Low"].iloc[-1], 4)
    level = rule.get("value")
    if not isinstance(level, (int, float)) or high is None or low is None:
        return {"triggered": False, "state": "unavailable", "detail": "Target and stop alerts need a numeric level and a candle"}
    if kind == "target_reached":
        direction = rule.get("direction", "bullish")
        triggered = high >= level if direction != "bearish" else low <= level
        return {"triggered": triggered, "state": "match" if triggered else "fail", "detail": "The bar reached the target level" if triggered else "The bar did not reach the target level"}
    direction = rule.get("direction", "bullish")
    triggered = low <= level if direction != "bearish" else high >= level
    return {"triggered": triggered, "state": "match" if triggered else "fail", "detail": "The bar reached the stop level" if triggered else "The bar did not reach the stop level"}


def _panel_trends(daily_df, intraday_frames):
    order = (
        ("Monthly", "1mo", None),
        ("Weekly", "1wk", None),
        ("Daily", "1d", None),
        ("4H", "4h", "4h"),
        ("1H", "1h", "1h"),
        ("15M", "15m", "15m"),
        ("5M", "5m", "5m"),
    )
    rows = []
    for label, chart_tf, intraday_key in order:
        if intraday_key:
            frame = intraday_frames.get(intraday_key)
            if frame is None or len(prepare_ohlcv(frame)) == 0:
                rows.append({"label": label, "status": "Unavailable", "detail": "No intraday candles"})
                continue
            trend = trend_from_frame(frame)
        elif chart_tf == "1d":
            trend = trend_from_frame(daily_df)
        else:
            trend = trend_from_frame(resample_chart(daily_df, chart_tf))
        rows.append({"label": label, "status": trend["status"], "detail": trend["detail"]})
    return rows


def _level_rows(price, zone_map):
    rows = []
    supply_rows = []
    specs = (
        ("demand", rows, (("1d", "Daily Demand"), ("1wk", "Weekly Demand"), ("1mo", "Monthly Demand"))),
        ("supply", supply_rows, (("1d", "Daily Supply"), ("1wk", "Weekly Supply"), ("1mo", "Monthly Supply"))),
    )
    for kind, bucket, labels in specs:
        for timeframe, label in labels:
            zone = zone_map.get(timeframe, {}).get(kind)
            distance = None if not zone else _zone_distance_pct(zone, price)
            bucket.append({
                "label": label,
                "kind": kind,
                "text": _range_text(zone),
                "score": None if not zone else zone.get("score"),
                "fresh": None if not zone else zone.get("fresh"),
                "status": None if not zone else zone.get("status"),
                "strength": None if not zone else zone.get("strength"),
                "tests": None if not zone else zone.get("tests"),
                "distance_pct": distance,
            })
    return rows, supply_rows


def intraday_zone_map(frames):
    """Zones on the supplied intraday candles. Daily bars are not substituted."""
    found = {}
    for key, frame in (frames or {}).items():
        source = prepare_ohlcv(frame).tail(400)
        if len(source) < 35:
            found[key] = {"demand": None, "supply": None}
            continue
        try:
            found[key] = sd_zones.frame_nearest(source, key)
        except Exception:
            continue
    return found


def vwap_from_intraday(intraday_frames):
    for key in ("5m", "15m", "1h"):
        frame = intraday_frames.get(key)
        if frame is None:
            continue
        enriched = add_indicators(frame, include_vwap=True)
        if enriched.empty or "vwap" not in enriched.columns:
            continue
        value = _num(enriched["vwap"].iloc[-1])
        if value is not None:
            return value
    return None


def analysis_payload(daily_df, display_df, timeframe, intraday_frames=None):
    """Snapshot, zone bands, and overlay series for one symbol.

    An empty intraday display is data-unavailable. Daily candles are not
    substituted for a requested intraday timeframe.
    """
    intraday_frames = intraday_frames or {}
    daily = prepare_ohlcv(daily_df)
    if daily.empty:
        return None
    intraday_chart = timeframe in {"5m", "15m", "1h", "4h", "6h", "12h"}
    display_source = prepare_ohlcv(display_df)
    if display_source.empty:
        if intraday_chart:
            return {
                "data_unavailable": True,
                "timeframe": timeframe,
                "message": (
                    f"No {timeframe} candles from the data provider. "
                    "Daily candles were not substituted."
                ),
            }
        return None
    display = add_indicators(display_source, include_vwap=intraday_chart)
    values = _snapshot_values(display)
    if intraday_chart:
        price = values.get("price")
        change_pct = values.get("change_pct")
        if values.get("vwap") is not None:
            values["vwap_basis"] = "session"
    else:
        daily_values = _snapshot_values(add_indicators(daily, include_vwap=False))
        price = daily_values.get("price")
        change_pct = daily_values.get("change_pct")
        session_vwap = vwap_from_intraday(intraday_frames)
        if session_vwap is not None:
            values["vwap"] = session_vwap
            values["vwap_basis"] = "session"
    zone_rectangles, zone_messages, zone_result = chart_zone_rectangles(daily, display.index)
    zone_map = zone_result["nearest"]
    intraday_zones = intraday_zone_map({
        key: frame for key, frame in intraday_frames.items() if key in {"5m", "15m", "1h", "4h"}
    })
    demand_rows, supply_rows = _level_rows(price, zone_map)
    overlays = overlay_series(display, include_vwap=intraday_chart)
    active = [
        CONDITION_LABELS[key]
        for key, flag in _last_patterns(display, price)[0].items()
        if flag is True and key in CONDITION_LABELS
    ]
    result = {
        "timeframe": timeframe,
        "price": price,
        "change_pct": change_pct,
        "trends": mtf_board(daily, intraday_frames, zone_map, intraday_zones),
        "technicals": technical_rows(values),
        "demand": demand_rows,
        "supply": supply_rows,
        "zones": zone_rectangles,
        "zone_messages": zone_messages,
        "zone_limits": zone_result["limits"],
        "overlays": overlays,
        "fib_levels": fib_level_rows(display),
        "patterns": active,
        "vwap_basis": values.get("vwap_basis"),
    }
    signal_frame = display
    signal_vwap = False
    if intraday_chart and values.get("vwap") is not None:
        signal_vwap = True
    elif values.get("vwap_basis") == "session" and values.get("vwap") is not None:
        signal_frame = display.copy()
        signal_frame.loc[signal_frame.index[-1], "vwap"] = values["vwap"]
        signal_vwap = True
    if intraday_chart:
        if timeframe in intraday_zones:
            signal_zones = intraday_zones[timeframe]
            zones_ready = True
        else:
            signal_zones = {"demand": None, "supply": None}
            zones_ready = False
    else:
        signal_zones = zone_map.get("1d")
        zones_ready = True
    from services.signal_engine import build_signal_from_enriched
    result["signal"] = build_signal_from_enriched(
        signal_frame,
        include_vwap=signal_vwap,
        zones=signal_zones,
        zones_calculated=zones_ready,
    )
    from services.trading_intelligence import assemble_report, quote_stats
    result["quote"] = quote_stats(daily)
    result["intelligence"] = assemble_report(
        signal_frame,
        result["signal"],
        signal_zones,
        result["trends"],
        include_vwap=signal_vwap,
        zones_calculated=zones_ready,
    )
    return result
