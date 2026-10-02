"""Accuracy checks for the chart backtest.

Every expected value is recalculated here from the candles up to the signal
bar (or from the stored database rows) instead of being copied from the run.
"""

import math
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from services.chart_backtest import (
    ConfigError,
    build_markers,
    clean_config,
    run_chart_backtest,
    zones_asof,
)
from services.paper_trading import PaperBook
from services.technical_analysis import (
    _candle_time,
    add_indicators,
    build_context,
    condition_state,
    nearest_zones,
    prepare_ohlcv,
    resample_chart,
    trend_from_frame,
)

ENTRY = ["price_above_ema20", "ema20_gt_ema50", "rsi_above_50"]


def synthetic_frame(bars=420, seed=7):
    rng = np.random.default_rng(seed)
    steps = rng.normal(0.0008, 0.018, bars)
    steps += 0.004 * np.sin(np.arange(bars) / 25)
    close = 100 * np.exp(np.cumsum(steps))
    opens = np.concatenate([[close[0]], close[:-1]]) * (1 + rng.normal(0, 0.004, bars))
    high = np.maximum(opens, close) * (1 + np.abs(rng.normal(0, 0.008, bars)))
    low = np.minimum(opens, close) * (1 - np.abs(rng.normal(0, 0.008, bars)))
    volume = rng.integers(80_000, 220_000, bars).astype(float)
    index = pd.date_range("2023-01-02", periods=bars, freq="B")
    return pd.DataFrame({"Open": opens, "High": high, "Low": low, "Close": close, "Volume": volume}, index=index)


def run(frame, **overrides):
    raw = {"entry_conditions": ENTRY, "exit_conditions": ["ema_cross_down"], "zone_levels": False}
    raw.update(overrides)
    return run_chart_backtest(frame, "1d", clean_config(raw, "1d"), symbol="TEST")


def comparable(trade):
    keys = ("signal_time", "entry_time", "entry", "stop_loss", "target_1", "target_2", "target_3",
            "exit_time", "exit", "exit_reason", "matched", "failed", "indicator_values")
    return {key: trade[key] for key in keys}


class ChartBacktestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.frame = synthetic_frame()
        cls.prepared = prepare_ohlcv(cls.frame)
        cls.result = run(cls.frame)
        cls.times = {_candle_time(stamp): position for position, stamp in enumerate(cls.prepared.index)}

    def test_strategy_produced_trades(self):
        self.assertFalse(self.result.get("data_unavailable"))
        self.assertGreaterEqual(self.result["total_trades"], 3)

    def test_no_look_ahead_when_future_candles_are_removed_or_changed(self):
        cut = 300
        cut_time = _candle_time(self.prepared.index[cut - 1])
        closed = [comparable(t) for t in self.result["trades"] if t["exit_time"] < cut_time]
        self.assertTrue(closed)
        truncated = run(self.frame.iloc[:cut])
        self.assertEqual(closed, [comparable(t) for t in truncated["trades"] if t["exit_time"] < cut_time])
        changed = self.frame.copy()
        rng = np.random.default_rng(99)
        factor = 1 + rng.normal(0, 0.05, len(changed) - cut)
        for column in ("Open", "High", "Low", "Close"):
            changed.iloc[cut:, changed.columns.get_loc(column)] *= factor
        changed.iloc[cut:, changed.columns.get_loc("Volume")] *= 3
        altered = run(changed)
        self.assertEqual(closed, [comparable(t) for t in altered["trades"] if t["exit_time"] < cut_time])

    def test_entry_fills_at_the_open_after_the_signal_bar(self):
        for trade in self.result["trades"]:
            signal = self.times[trade["signal_time"]]
            entry = self.times[trade["entry_time"]]
            self.assertEqual(entry, signal + 1)
            self.assertAlmostEqual(trade["entry"], round(float(self.prepared["Open"].iloc[entry]), 2), places=2)
            self.assertGreater(trade["exit_time"], trade["signal_time"])

    def test_reasons_are_only_conditions_true_on_the_signal_bar(self):
        for trade in self.result["trades"][:4]:
            signal = self.times[trade["signal_time"]]
            context = build_context(self.prepared.iloc[:signal + 1], include_zones=False)
            for detail in trade["conditions"]:
                self.assertEqual(detail["state"], condition_state(detail["id"], context), detail["id"])
            self.assertEqual(trade["matched"], [d["label"] for d in trade["conditions"] if d["state"] == "match"])
            self.assertEqual(len(trade["matched"]), len(ENTRY))

    def test_indicator_values_match_a_recalculation_on_the_prefix(self):
        for trade in self.result["trades"][:4]:
            signal = self.times[trade["signal_time"]]
            row = add_indicators(self.prepared.iloc[:signal + 1]).iloc[-1]
            for key in ("rsi", "macd", "ema20", "ema50", "atr", "adx"):
                expected = row[key]
                actual = trade["indicator_values"][key]
                if pd.isna(expected):
                    self.assertIsNone(actual)
                else:
                    self.assertAlmostEqual(actual, round(float(expected), 2), places=2, msg=key)

    def test_weekly_and_monthly_trend_match_resampled_prefix(self):
        result = run(self.frame, entry_conditions=["weekly_bullish", "price_above_ema20"])
        self.assertTrue(result["trades"])
        for trade in result["trades"][:4]:
            signal = self.times[trade["signal_time"]]
            prefix = self.prepared.iloc[:signal + 1]
            self.assertEqual(trend_from_frame(resample_chart(prefix, "1wk"))["status"], "Bullish")
            self.assertEqual(trade["matched"], ["Weekly bullish", "Price above EMA 20"])

    def test_risk_multiple_targets_and_swing_low_stop(self):
        for trade in self.result["trades"]:
            signal = self.times[trade["signal_time"]]
            swing = float(self.prepared["Low"].iloc[signal - 9:signal + 1].min())
            self.assertAlmostEqual(trade["stop_loss"], round(swing, 2), places=2)
            risk = trade["entry"] - swing
            self.assertGreater(risk, 0)
            for factor, key in ((1, "target_1"), (2, "target_2"), (3, "target_3")):
                self.assertAlmostEqual(trade[key], trade["entry"] + factor * risk, delta=0.011 * (factor + 1))

    def test_percent_and_atr_methods(self):
        percent = run(self.frame, target_method="percent", target_values=[2, 4, 6], stop_method="percent", stop_value=3)
        for trade in percent["trades"]:
            self.assertAlmostEqual(trade["stop_loss"], round(trade["entry"] * 0.97, 2), delta=0.011)
            self.assertAlmostEqual(trade["target_1"], round(trade["entry"] * 1.02, 2), delta=0.011)
            self.assertAlmostEqual(trade["target_3"], round(trade["entry"] * 1.06, 2), delta=0.011)
        atr = run(self.frame, target_method="atr", target_values=[1, 2, 3], stop_method="atr", stop_value=1.5)
        self.assertTrue(atr["trades"])
        for trade in atr["trades"]:
            signal = self.times[trade["signal_time"]]
            value = float(add_indicators(self.prepared.iloc[:signal + 1])["atr"].iloc[-1])
            self.assertAlmostEqual(trade["stop_loss"], round(trade["entry"] - 1.5 * value, 2), delta=0.011)
            self.assertAlmostEqual(trade["target_2"], round(trade["entry"] + 2 * value, 2), delta=0.011)

    def test_exit_prices_follow_the_candles(self):
        for trade in self.result["trades"]:
            bar = self.prepared.iloc[self.times[trade["exit_time"]]]
            code = trade["exit_reason_code"]
            if code == "target":
                self.assertGreaterEqual(float(bar["High"]) + 0.01, trade["exit"])
                self.assertGreater(float(bar["Low"]), trade["final_stop"], "stop must win when both are touched")
            elif code in {"stop", "trailing_stop"}:
                self.assertLessEqual(float(bar["Low"]) - 0.01, trade["exit"])
                self.assertAlmostEqual(trade["exit"], trade["final_stop"], delta=0.011)
            elif code in {"stop_gap", "target_gap", "exit_condition"}:
                self.assertAlmostEqual(trade["exit"], round(float(bar["Open"]), 2), places=2)
            if code == "exit_condition":
                enriched = add_indicators(self.prepared.iloc[:self.times[trade["exit_time"]]])
                now, before = enriched.iloc[-1], enriched.iloc[-2]
                self.assertTrue(now["ema20"] < now["ema50"] and before["ema20"] >= before["ema50"])
            expected_pnl = trade["quantity"] * (trade["exit"] - trade["entry"])
            self.assertAlmostEqual(trade["pnl"], expected_pnl, delta=0.05 + trade["quantity"] * 0.011)

    def test_markers_use_chart_candle_times(self):
        candle_times = set(self.times)
        for marker in self.result["markers"]:
            self.assertIn(marker["time"], candle_times)
        entries = [m for m in self.result["markers"] if m["kind"] == "entry"]
        self.assertEqual([m["time"] for m in entries], [t["entry_time"] for t in self.result["trades"]])

    def test_markers_match_the_stored_backtest(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            book = PaperBook(Path(folder) / "book.sqlite")
            saved = book.save_backtest(dict(self.result))
            loaded = book.load_backtest(saved["run_id"])
        self.assertEqual(len(loaded["trades"]), self.result["total_trades"])
        rebuilt = build_markers(loaded["trades"], loaded["market_events"])
        self.assertEqual(rebuilt, self.result["markers"])
        self.assertEqual([comparable(t) for t in loaded["trades"]], [comparable(t) for t in self.result["trades"]])

    def test_audit_trail_fields(self):
        required = {"stock", "timeframe", "timestamp", "price", "signal_type", "triggered_conditions",
                    "indicator_values", "demand_zone", "supply_zone", "target", "stop_loss", "exit_reason"}
        self.assertEqual(len(self.result["audit"]), self.result["total_trades"])
        for record, trade in zip(self.result["audit"], self.result["trades"]):
            self.assertTrue(required.issubset(record))
            self.assertEqual(record["triggered_conditions"], trade["matched"])
            self.assertEqual(record["timestamp"], trade["signal_time"])

    def test_watch_list_is_not_a_prediction(self):
        current = self.result["current"]
        self.assertIsNotNone(current)
        text = " ".join(item["label"] + " " + str(item["detail"]) for item in current["watch"]).lower()
        for word in ("guarantee", "will rise", "buy now", "sell now", "predict"):
            self.assertNotIn(word, text)

    def test_invalid_requests_are_rejected(self):
        with self.assertRaises(ConfigError):
            clean_config({"entry_conditions": []}, "1d")
        with self.assertRaises(ConfigError):
            clean_config({"entry_conditions": ["daily_demand"], "entry_logic": "OR"}, "1d")
        with self.assertRaises(ConfigError):
            clean_config({"entry_conditions": ["daily_demand"]}, "1h")
        with self.assertRaises(ConfigError):
            clean_config({"entry_conditions": ENTRY, "target_values": [3, 2, 1]}, "1d")


class ZoneAsOfTests(unittest.TestCase):
    def test_zones_use_only_candles_up_to_the_signal(self):
        frame = prepare_ohlcv(synthetic_frame(bars=360, seed=11))
        prefix = frame.iloc[:330]
        found = zones_asof(prefix)
        self.assertEqual(found, nearest_zones(prefix))
        changed = frame.copy()
        changed.iloc[330:, :4] *= 1.3
        self.assertEqual(zones_asof(changed.iloc[:330]), found)


if __name__ == "__main__":
    unittest.main()
