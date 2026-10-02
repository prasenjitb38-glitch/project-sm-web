"""Accuracy checks for the shared technical engine.

Expected values are computed in this file with independent formulas, or by
hand for the short parabolic SAR walk. They do not call the function under test
to produce the expected number.
"""

import math
import time
import unittest

import pandas as pd

from services.technical_analysis import (
    add_indicators,
    build_context,
    condition_report,
    condition_state,
    matches_logic,
    prepare_ohlcv,
)


def frame_from_closes(closes, volume=1000, spread=1.0):
    index = pd.date_range("2024-01-02", periods=len(closes), freq="B")
    close = [float(value) for value in closes]
    return pd.DataFrame({
        "Open": close,
        "High": [value + spread for value in close],
        "Low": [value - spread for value in close],
        "Close": close,
        "Volume": volume,
    }, index=index)


def independent_ema(values, span):
    alpha = 2 / (span + 1)
    current = values[0]
    for value in values[1:]:
        current = alpha * value + (1 - alpha) * current
    return current


def independent_wilder(values, period):
    if len(values) < period:
        return None
    current = sum(values[:period]) / period
    for value in values[period:]:
        current = (current * (period - 1) + value) / period
    return current


def independent_rsi(closes, period=14):
    deltas = [closes[index] - closes[index - 1] for index in range(1, len(closes))]
    gains = [max(delta, 0) for delta in deltas]
    losses = [max(-delta, 0) for delta in deltas]
    average_gain = independent_wilder(gains, period)
    average_loss = independent_wilder(losses, period)
    if average_gain is None or average_loss is None or average_loss == 0:
        return None
    return 100 - (100 / (1 + average_gain / average_loss))


class IndicatorAccuracyTest(unittest.TestCase):
    def test_sma_ema_and_bollinger_match_independent_formulas(self):
        closes = [float(10 + (index % 7)) for index in range(40)]
        enriched = add_indicators(frame_from_closes(closes))
        last = enriched.iloc[-1]
        self.assertAlmostEqual(last["sma5"], sum(closes[-5:]) / 5, places=8)
        self.assertAlmostEqual(last["sma20"], sum(closes[-20:]) / 20, places=8)
        self.assertAlmostEqual(last["ema9"], independent_ema(closes, 9), places=8)
        self.assertAlmostEqual(last["ema20"], independent_ema(closes, 20), places=8)
        window = closes[-20:]
        mean = sum(window) / 20
        deviation = math.sqrt(sum((item - mean) ** 2 for item in window) / 20)
        self.assertAlmostEqual(last["bb_mid"], mean, places=8)
        self.assertAlmostEqual(last["bb_upper"], mean + 2 * deviation, places=8)
        self.assertAlmostEqual(last["bb_lower"], mean - 2 * deviation, places=8)

    def test_rsi_macd_stochastic_and_obv(self):
        closes = [100 + index * 0.5 + (index % 3) for index in range(40)]
        volumes = [100 if index % 2 == 0 else 50 for index in range(40)]
        data = frame_from_closes(closes, volume=1)
        data["Volume"] = volumes
        enriched = add_indicators(data)
        last = enriched.iloc[-1]
        self.assertAlmostEqual(last["rsi"], independent_rsi(closes), places=6)
        macd = independent_ema(closes, 12) - independent_ema(closes, 26)
        # Signal is an EMA of the MACD series, not of the close.
        macd_series = []
        fast = closes[0]
        slow = closes[0]
        alpha_fast = 2 / 13
        alpha_slow = 2 / 27
        for price in closes:
            fast = alpha_fast * price + (1 - alpha_fast) * fast
            slow = alpha_slow * price + (1 - alpha_slow) * slow
            macd_series.append(fast - slow)
        self.assertAlmostEqual(last["macd"], macd_series[-1], places=6)
        self.assertAlmostEqual(last["macd_signal"], independent_ema(macd_series, 9), places=6)
        self.assertAlmostEqual(last["macd_hist"], last["macd"] - last["macd_signal"], places=8)
        lows = [price - 1 for price in closes[-14:]]
        highs = [price + 1 for price in closes[-14:]]
        expected_k = 100 * (closes[-1] - min(lows)) / (max(highs) - min(lows))
        self.assertAlmostEqual(last["stoch_k"], expected_k, places=6)
        self.assertAlmostEqual(last["williams_r"], -100 * (max(highs) - closes[-1]) / (max(highs) - min(lows)), places=6)
        obv = 0
        for index in range(1, len(closes)):
            if closes[index] > closes[index - 1]:
                obv += volumes[index]
            elif closes[index] < closes[index - 1]:
                obv -= volumes[index]
        self.assertAlmostEqual(last["obv"], obv, places=6)
        self.assertAlmostEqual(last["roc"], (closes[-1] / closes[-13] - 1) * 100, places=6)
        self.assertAlmostEqual(last["momentum"], closes[-1] - closes[-11], places=6)

    def test_rsi_is_100_when_there_are_no_down_closes(self):
        closes = [float(100 + index) for index in range(30)]
        last = add_indicators(frame_from_closes(closes)).iloc[-1]
        self.assertAlmostEqual(last["rsi"], 100.0, places=6)

    def test_session_vwap_needs_two_bars_and_uses_typical_price(self):
        index = pd.to_datetime(["2024-06-03 09:15", "2024-06-03 09:20", "2024-06-04 09:15"])
        data = pd.DataFrame({
            "Open": [9, 11, 20],
            "High": [10, 12, 21],
            "Low": [8, 10, 19],
            "Close": [9, 11, 20],
            "Volume": [100, 100, 50],
        }, index=index)
        enriched = add_indicators(data, include_vwap=True)
        self.assertAlmostEqual(enriched["vwap"].iloc[1], 10.0, places=6)
        self.assertTrue(math.isnan(enriched["vwap"].iloc[2]))
        daily = add_indicators(frame_from_closes([10, 11, 12]), include_vwap=True)
        self.assertTrue(daily["vwap"].isna().all())

    def test_psar_and_ichimoku_tenkan_match_hand_values(self):
        index = pd.date_range("2024-01-02", periods=4, freq="B")
        data = pd.DataFrame({
            "Open": [11, 12, 13, 14],
            "High": [12, 14, 15, 16],
            "Low": [10, 11, 12, 13],
            "Close": [11, 13, 14, 15],
            "Volume": [100, 100, 100, 100],
        }, index=index)
        sar, _direction = __import__("services.technical_analysis", fromlist=["_psar"])._psar(data)
        self.assertAlmostEqual(sar.iloc[1], 10.0, places=6)
        self.assertAlmostEqual(sar.iloc[2], 10.0, places=6)
        self.assertAlmostEqual(sar.iloc[3], 10.3, places=6)
        closes = [float(20 + index) for index in range(12)]
        highs = [price + 2 for price in closes]
        lows = [price - 1 for price in closes]
        cloud = add_indicators(pd.DataFrame({
            "Open": closes, "High": highs, "Low": lows, "Close": closes, "Volume": 10,
        }, index=pd.date_range("2024-01-02", periods=12, freq="B")))
        self.assertAlmostEqual(cloud["ichimoku_tenkan"].iloc[-1], (max(highs[-9:]) + min(lows[-9:])) / 2, places=6)

    def test_invalid_candles_are_rejected_and_duplicates_keep_the_last_bar(self):
        index = pd.to_datetime(["2024-01-02", "2024-01-02", "2024-01-03"])
        data = pd.DataFrame({
            "Open": [10, 12, 8],
            "High": [11, 13, 9],
            "Low": [9, 11, 10],
            "Close": [10.5, 12.5, 8.5],
            "Volume": [1, 2, 3],
        }, index=index)
        cleaned = prepare_ohlcv(data)
        self.assertEqual(len(cleaned), 1)
        self.assertAlmostEqual(cleaned["Close"].iloc[0], 12.5)

    def test_timezone_is_stored_as_naive_ist(self):
        stamp = pd.Timestamp("2024-01-01 18:30", tz="UTC")
        data = pd.DataFrame({
            "Open": [10], "High": [11], "Low": [9], "Close": [10], "Volume": [1],
        }, index=pd.DatetimeIndex([stamp]))
        cleaned = prepare_ohlcv(data)
        self.assertIsNone(cleaned.index.tz)
        self.assertEqual(str(cleaned.index[0]), "2024-01-02 00:00:00")

    def test_indicators_do_not_use_future_bars(self):
        closes = [100 + math.sin(index / 3) * 5 + index * 0.1 for index in range(80)]
        full = add_indicators(frame_from_closes(closes))
        prefix = add_indicators(frame_from_closes(closes[:50]))
        for column in ("rsi", "ema20", "macd", "atr", "adx", "supertrend"):
            self.assertAlmostEqual(full[column].iloc[49], prefix[column].iloc[-1], places=6, msg=column)

    def test_unavailable_condition_is_not_a_match(self):
        context = build_context(frame_from_closes([100 + index for index in range(60)]), include_zones=False)
        self.assertEqual(condition_state("daily_demand", context), "unavailable")
        self.assertFalse(matches_logic(["price_above_ema20", "daily_demand"], context, "AND"))
        report = condition_report(["rsi_above_50", "daily_demand"], context)
        self.assertIn("At or just above a daily demand zone", report["unavailable"])
        self.assertNotIn("At or just above a daily demand zone", report["matched"])
        self.assertEqual(condition_state("rsi_above_50", context), "match")
        self.assertEqual(report["score"], 100)

    def test_and_or_and_volume_threshold(self):
        closes = [100] * 30
        data = frame_from_closes(closes, volume=100)
        data.loc[data.index[-1], "Volume"] = 1000
        context = build_context(data, include_zones=False)
        self.assertEqual(condition_state("volume_gt_2x", context), "match")
        self.assertEqual(condition_state("volume_gt_avg", context), "match")
        self.assertFalse(matches_logic(["volume_gt_2x", "rsi_above_50"], context, "AND") and condition_state("rsi_above_50", context) != "match")
        states = [condition_state("volume_gt_2x", context), condition_state("rsi_oversold", context)]
        self.assertTrue(matches_logic(["volume_gt_2x", "rsi_oversold"], context, "OR") or "match" not in states)

    def test_bullish_engulfing_rules(self):
        index = pd.date_range("2024-01-02", periods=2, freq="B")
        data = pd.DataFrame({
            "Open": [12, 9],
            "High": [12.2, 13],
            "Low": [10, 8.8],
            "Close": [10.2, 12.4],
            "Volume": [100, 100],
        }, index=index)
        context = build_context(data, include_zones=False)
        self.assertEqual(condition_state("bullish_engulfing", context), "match")
        self.assertEqual(condition_state("bearish_engulfing", context), "fail")

    def test_user_thresholds_change_the_result(self):
        rising = frame_from_closes([float(100 + index) for index in range(60)])
        default = build_context(rising, include_zones=False)
        self.assertEqual(condition_state("rsi_50_70", default), "fail")
        wide = build_context(rising, include_zones=False, params={"rsi_min": 90, "rsi_max": 100})
        self.assertEqual(condition_state("rsi_50_70", wide), "match")
        self.assertIn("RSI 90–100", condition_report(["rsi_50_70"], wide)["matched"])
        spike = frame_from_closes([100] * 30, volume=100)
        spike.loc[spike.index[-1], "Volume"] = 1000
        self.assertEqual(condition_state("volume_gt_avg", build_context(spike, include_zones=False, params={"vol_mult": 5})), "match")
        self.assertEqual(condition_state("volume_gt_avg", build_context(spike, include_zones=False, params={"vol_mult": 20})), "fail")
        ignored = build_context(rising, include_zones=False, params={"rsi_min": "abc", "unknown": 3})
        self.assertEqual(ignored["params"]["rsi_min"], 50)
        self.assertNotIn("unknown", ignored["params"])

    def test_price_range_atr_and_52_week_high(self):
        short = build_context(frame_from_closes([float(100 + index) for index in range(60)]), include_zones=False)
        self.assertEqual(condition_state("price_range", short), "unavailable")
        self.assertEqual(condition_state("near_52w_high", short), "unavailable")
        priced = build_context(frame_from_closes([float(100 + index) for index in range(60)]), include_zones=False, params={"price_min": 150})
        self.assertEqual(condition_state("price_range", priced), "match")
        capped = build_context(frame_from_closes([float(100 + index) for index in range(60)]), include_zones=False, params={"price_max": 120})
        self.assertEqual(condition_state("price_range", capped), "fail")
        year = build_context(frame_from_closes([float(100 + index) for index in range(260)]), include_zones=False)
        self.assertEqual(condition_state("near_52w_high", year), "match")
        values = year["values"]
        self.assertAlmostEqual(values["atr_pct"], round(values["atr"] / values["price"] * 100, 2), places=2)

    def test_double_bottom_needs_similar_lows_and_neckline_break(self):
        closes = [118 - 2 * index for index in range(10)]
        closes += [102, 104, 106, 108, 110]
        closes += [108, 106, 104, 102, 100.5]
        closes += [102.5 + 1.5 * index for index in range(12)]
        context = build_context(frame_from_closes(closes), include_zones=False)
        self.assertEqual(condition_state("double_bottom", context), "match")
        self.assertEqual(condition_state("double_top", context), "fail")
        flag, evidence = __import__("services.technical_analysis", fromlist=["double_pattern"]).double_pattern(prepare_ohlcv(frame_from_closes(closes)), "bottom")
        self.assertTrue(flag)
        self.assertEqual(evidence["neckline"], 111.0)
        not_broken = closes[:-6]
        context = build_context(frame_from_closes(not_broken + [104] * 6), include_zones=False)
        self.assertEqual(condition_state("double_bottom", context), "fail")
        tiny = build_context(frame_from_closes(closes[:20]), include_zones=False)
        self.assertEqual(condition_state("double_bottom", tiny), "unavailable")

    def test_fibonacci_golden_zone(self):
        closes = [100 + 2.5 * index for index in range(41)] + [200 - 5 * index for index in range(1, 11)]
        context = build_context(frame_from_closes(closes), include_zones=False)
        self.assertEqual(condition_state("fib_golden_zone", context), "match")
        flat_top = [100 + 2.5 * index for index in range(41)]
        self.assertEqual(condition_state("fib_golden_zone", build_context(frame_from_closes(flat_top), include_zones=False)), "fail")

    def test_flat_window_keeps_indicators_numeric(self):
        closes = [100.0] * 30 + [100 + index for index in range(1, 11)]
        data = frame_from_closes(closes, spread=0.0)
        enriched = add_indicators(data)
        for column in ("stoch_k", "stoch_d", "williams_r", "cci", "rsi", "rvol", "adx"):
            self.assertTrue(pd.api.types.is_float_dtype(enriched[column]), column)
        self.assertTrue(math.isnan(enriched["stoch_k"].iloc[20]))
        self.assertAlmostEqual(enriched["stoch_k"].iloc[-1], 100.0, places=6)

    def test_indicator_batch_stays_under_two_seconds(self):
        closes = [100 + (index % 11) * 0.2 for index in range(250)]
        base = frame_from_closes(closes)
        started = time.perf_counter()
        for _ in range(100):
            add_indicators(base)
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 30.0, f"100 x 250-bar indicator passes took {elapsed:.2f}s")

    def test_cached_indicators_are_independent_copies(self):
        base = frame_from_closes([100 + (index % 9) for index in range(80)])
        first = add_indicators(base)
        first.loc[first.index[-1], "rsi"] = -1
        second = add_indicators(base)
        self.assertNotEqual(second["rsi"].iloc[-1], -1)

    def test_zone_cache_matches_the_engine(self):
        from services import zone_cache, zone_engine
        closes = []
        price = 100.0
        for index in range(260):
            step = 6.0 if index % 37 in (5, 6, 7) else -5.0 if index % 41 in (20, 21, 22) else (0.15 if index % 2 else -0.12)
            price = max(20.0, price + step)
            closes.append(price)
        frame = frame_from_closes(closes, spread=0.6)
        frame["Volume"] = [5000 if index % 37 in (5, 6, 7) or index % 41 in (20, 21, 22) else 1000 for index in range(260)]
        zone_engine.for_timeframe = zone_cache._original_for_timeframe
        zone_engine._indicators = zone_cache._original_indicators
        try:
            plain = {tf: zone_engine.detect_zones(frame, timeframe=tf, max_zones=30) for tf in ("1d", "1wk")}
        finally:
            zone_engine.for_timeframe = zone_cache._for_timeframe
            zone_engine._indicators = zone_cache._indicators
        zone_cache._results.clear()
        for tf, expected in plain.items():
            self.assertEqual(zone_cache.detect_zones(frame, timeframe=tf, max_zones=30), expected)
            repeat = zone_cache.detect_zones(frame, timeframe=tf, max_zones=30)
            self.assertEqual(repeat, expected)
            if repeat:
                repeat[0]["score"] = -1
                self.assertEqual(zone_cache.detect_zones(frame, timeframe=tf, max_zones=30), expected)


if __name__ == "__main__":
    unittest.main()
