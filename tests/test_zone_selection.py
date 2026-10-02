"""Demand & Supply zone engine: detection, strength, freshness, selection,
confluence and broken-zone removal. Candles are built bar by bar so every
expected value can be read straight from the test data."""

import unittest

import numpy as np
import pandas as pd

from services import sd_zones
from services.chart_backtest import zones_asof
from services.technical_analysis import nearest_zones


def bar(open_, close, spread=0.3, volume=1000.0):
    return (open_, max(open_, close) + spread, min(open_, close) - spread, close, volume)


def noise(level, count):
    return [bar(level, level + 0.4) if i % 2 == 0 else bar(level + 0.4, level) for i in range(count)]


def demand_bars():
    """Flat noise, a drop, a two-candle base at 96.85-97.2, then a rally."""
    rows = noise(100, 40)
    rows += [bar(100, 99), bar(99, 98), bar(98, 97.2)]
    rows += [bar(97.2, 97.0, 0.15), bar(97.0, 97.1, 0.15)]
    rows += [bar(97.1, 99.6, 0.2, 3000.0), bar(99.6, 100.5), bar(100.5, 101.0)]
    rows += noise(101, 20)
    return rows


def frame(rows):
    index = pd.date_range("2024-01-01", periods=len(rows), freq="B")
    return pd.DataFrame(rows, columns=["Open", "High", "Low", "Close", "Volume"], index=index)


def mirror(rows, pivot=200.0):
    return [(pivot - o, pivot - lo, pivot - h, pivot - c, v) for o, h, lo, c, v in rows]


def find(zones, kind, bottom, top):
    for zone in zones:
        if zone["type"] == kind and abs(zone["bottom"] - bottom) < 1e-6 and abs(zone["top"] - top) < 1e-6:
            return zone
    return None


def zone(kind, bottom, top, timeframe="1d", score=70, broken=False, tests=0, departure=2.0, time=0):
    freshness = sd_zones.freshness_label(tests, 0.0, broken)
    return {
        "type": kind, "timeframe": timeframe, "bottom": bottom, "top": top, "score": score,
        "broken": broken, "tests": tests, "departure_atr": departure, "time": time,
        "freshness": freshness, "strength": sd_zones.strength_label(score), "pattern": "DBR",
        "pattern_name": "Drop-Base-Rally", "base_candles": 2, "volume_ratio": 1.6,
        "volume_confirmed": True, "bos": True, "penetration": 0.0, "formed_time": time,
    }


class DetectionTests(unittest.TestCase):
    def test_drop_base_rally_demand(self):
        found = find(sd_zones.detect(frame(demand_bars()), "1d"), "demand", 96.85, 97.2)
        self.assertIsNotNone(found)
        self.assertEqual(found["pattern"], "DBR")
        self.assertEqual(found["pattern_name"], "Drop-Base-Rally")
        self.assertEqual(found["base_candles"], 2)
        self.assertGreaterEqual(found["departure_atr"], sd_zones.MIN_DEPARTURE_ATR)
        self.assertEqual(found["volume_ratio"], 3.0)
        self.assertTrue(found["volume_confirmed"])
        self.assertEqual(found["time"], int(pd.Timestamp(frame(demand_bars()).index[43]).timestamp()))

    def test_rally_base_drop_supply_is_the_mirror(self):
        found = find(sd_zones.detect(frame(mirror(demand_bars())), "1d"), "supply", 200 - 97.2, 200 - 96.85)
        self.assertIsNotNone(found)
        self.assertEqual(found["pattern_name"], "Rally-Base-Drop")
        self.assertEqual(found["freshness"], "Fresh")

    def test_no_departure_means_no_zone(self):
        rows = noise(100, 40) + [bar(100, 99), bar(99, 98), bar(98, 97.2)]
        rows += [bar(97.2, 97.0, 0.15), bar(97.0, 97.1, 0.15), bar(97.1, 97.6), bar(97.6, 97.3)] + noise(97.3, 10)
        self.assertIsNone(find(sd_zones.detect(frame(rows), "1d"), "demand", 96.85, 97.2))

    def test_missing_volume_is_unavailable_not_confirmed(self):
        rows = [(o, h, lo, c, 0.0) for o, h, lo, c, _ in demand_bars()]
        found = find(sd_zones.detect(frame(rows), "1d"), "demand", 96.85, 97.2)
        self.assertIsNone(found["volume_ratio"])
        self.assertIsNone(found["volume_confirmed"])
        self.assertIn("Volume confirmation unavailable (no volume data)", sd_zones.reasons(found))

    def test_short_history_returns_nothing(self):
        self.assertEqual(sd_zones.detect(frame(noise(100, 12)), "1d"), [])


class StrengthTests(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(sd_zones.strength_label(70), "Strong")
        self.assertEqual(sd_zones.strength_label(69), "Moderate")
        self.assertEqual(sd_zones.strength_label(50), "Moderate")
        self.assertEqual(sd_zones.strength_label(49), "Weak")

    def test_components(self):
        score, parts = sd_zones.score_zone(4.0, 1.6, True, 2, False, "Fresh", 0)
        self.assertEqual(parts, {"departure": 30.0, "volume": 15, "bos": 15, "base": 10, "freshness": 20, "reactions": 0})
        self.assertEqual(score, 90)
        weaker, _ = sd_zones.score_zone(1.5, None, False, 4, False, "Weakening", 2)
        self.assertEqual(weaker, round(1.5 / 4 * 30) + 0 + 0 + 6 + 4 + 8)
        self.assertLess(weaker, score)

    def test_detected_score_matches_its_parts(self):
        found = find(sd_zones.detect(frame(demand_bars()), "1d"), "demand", 96.85, 97.2)
        expected, _ = sd_zones.score_zone(found["departure_atr"], found["volume_ratio"], found["bos"],
                                          found["base_candles"], False, found["freshness"], found["tests"])
        self.assertEqual(found["score"], expected)
        self.assertEqual(found["strength"], sd_zones.strength_label(expected))


class FreshnessTests(unittest.TestCase):
    def detect_after(self, extra):
        return find(sd_zones.detect(frame(demand_bars() + extra), "1d"), "demand", 96.85, 97.2)

    def test_fresh_tested_weakening_broken(self):
        self.assertEqual(self.detect_after([])["freshness"], "Fresh")
        shallow = [bar(101, 99.5, 0.1), bar(99.5, 97.3, 0.15), bar(97.3, 99.5, 0.1), bar(99.5, 101, 0.1)]
        tested = self.detect_after(shallow)
        self.assertEqual((tested["tests"], tested["freshness"]), (1, "Tested"))
        twice = self.detect_after(shallow + shallow)
        self.assertEqual((twice["tests"], twice["freshness"]), (2, "Weakening"))
        broken = self.detect_after([bar(101, 99, 0.1), bar(99, 96.5, 0.1)])
        self.assertTrue(broken["broken"])
        self.assertEqual(broken["freshness"], "Broken")

    def test_deep_single_test_is_weakening(self):
        deep = [bar(101, 99.5, 0.1), bar(99.5, 97.0, 0.05), bar(97.0, 100, 0.1)]
        self.assertEqual(self.detect_after(deep)["freshness"], "Weakening")

    def test_lifecycle_counts_separate_visits_until_the_break(self):
        highs = np.array([105, 99, 105, 99, 105, 99, 105], float)
        lows = np.array([101, 97.5, 101, 97.5, 101, 95, 101], float)
        closes = np.array([104, 98.5, 104, 98.5, 104, 95.5, 104], float)
        life = sd_zones.lifecycle("demand", 98.0, 96.0, highs, lows, closes)
        self.assertEqual(life, {"tests": 2, "penetration": 0.25, "broken": True})


class SelectionTests(unittest.TestCase):
    def test_nearest_valid_zone_on_each_side(self):
        zones = [
            zone("demand", 95, 97), zone("demand", 90, 92), zone("demand", 85, 87),
            zone("demand", 98, 99, score=30),
            zone("demand", 97.5, 98.5, broken=True),
            zone("supply", 103, 105), zone("supply", 120, 125),
            zone("supply", 101, 101.5, departure=1.2),
        ]
        picked = sd_zones.select_nearby(zones, 100.0, 12.0, per_side=2)
        self.assertEqual([(z["bottom"], z["top"]) for z in picked["demand"]], [(95, 97), (90, 92)])
        self.assertEqual([(z["bottom"], z["top"]) for z in picked["supply"]], [(103, 105)])

    def test_zone_on_the_wrong_side_is_ignored(self):
        picked = sd_zones.select_nearby([zone("demand", 101, 103), zone("supply", 95, 97)], 100.0, 12.0)
        self.assertEqual(picked, {"demand": [], "supply": []})

    def test_price_inside_a_zone_has_zero_distance(self):
        inside = zone("demand", 99, 101)
        self.assertEqual(sd_zones.distance_pct(inside, 100.0), 0.0)
        self.assertEqual(sd_zones.select_nearby([inside], 100.0, 3.0)["demand"], [inside])

    def test_distance_limit_adapts_to_volatility(self):
        calm = frame(noise(100, 60))
        wild = frame([bar(100, 104, 1.0) if i % 2 == 0 else bar(104, 100, 1.0) for i in range(60)])
        calm_atr, calm_limits = sd_zones.distance_limits(calm, 100.0)
        wild_atr, wild_limits = sd_zones.distance_limits(wild, 100.0)
        self.assertLess(calm_atr, wild_atr)
        self.assertEqual(calm_limits["1d"], 5.0)
        self.assertGreater(wild_limits["1d"], calm_limits["1d"])
        self.assertLessEqual(wild_limits["1d"], 12.0)
        self.assertLess(calm_limits["1d"], calm_limits["1wk"])
        self.assertLess(calm_limits["1wk"], calm_limits["1mo"])

    def test_analyze_reports_missing_side_instead_of_inventing_one(self):
        result = sd_zones.analyze(frame(demand_bars()), ("1d",))
        demand = result["nearest"]["1d"]["demand"]
        self.assertEqual((demand["bottom"], demand["top"]), (96.85, 97.2))
        self.assertIsNone(result["nearest"]["1d"]["supply"])
        self.assertIsNone(result["messages"]["demand"])
        self.assertEqual(result["messages"]["supply"], "No nearby valid Supply Zone")
        self.assertEqual({z["type"] for z in result["display"]}, {"demand"})

    def test_broken_zone_is_removed_from_the_chart(self):
        broken_rows = demand_bars() + [bar(101, 99, 0.1), bar(99, 96.5, 0.1)] + noise(96.5, 6)
        result = sd_zones.analyze(frame(broken_rows), ("1d",))
        self.assertFalse(any(z["bottom"] == 96.85 and z["top"] == 97.2 for z in result["display"]))


class ConfluenceTests(unittest.TestCase):
    def public(self, item):
        return sd_zones._public(item, 100.0)

    def test_overlapping_timeframes_merge(self):
        daily = self.public(zone("demand", 96, 98, "1d", score=60))
        weekly = self.public(zone("demand", 95, 97.5, "1wk", score=72))
        supply = self.public(zone("supply", 104, 106, "1d"))
        merged = sd_zones.merge_confluence([daily, weekly, supply])
        self.assertEqual(len(merged), 2)
        both = next(item for item in merged if item["confluence"])
        self.assertEqual(both["label"], "Daily + Weekly Demand Confluence")
        self.assertEqual((both["bottom"], both["top"]), (95, 98))
        self.assertEqual(both["timeframes"], ["1d", "1wk"])
        self.assertEqual(both["score"], 77)
        self.assertEqual(both["strength"], "Strong")
        self.assertEqual(len(both["members"]), 2)
        single = next(item for item in merged if not item["confluence"])
        self.assertEqual(single["label"], "Daily Supply")

    def test_no_merge_without_overlap_or_within_one_timeframe(self):
        merged = sd_zones.merge_confluence([
            self.public(zone("demand", 96, 98, "1d")),
            self.public(zone("demand", 90, 92, "1wk")),
            self.public(zone("demand", 97, 99, "1d")),
            self.public(zone("supply", 97, 99, "1mo")),
        ])
        self.assertEqual(len(merged), 4)
        self.assertFalse(any(item["confluence"] for item in merged))

    def test_three_timeframe_confluence(self):
        merged = sd_zones.merge_confluence([
            self.public(zone("supply", 104, 106, "1d", score=55)),
            self.public(zone("supply", 105, 108, "1wk", score=60)),
            self.public(zone("supply", 103, 105.5, "1mo", score=58)),
        ])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["label"], "Daily + Weekly + Monthly Supply Confluence")
        self.assertEqual((merged[0]["bottom"], merged[0]["top"]), (103, 108))


class RenderingTests(unittest.TestCase):
    def test_rectangles_start_near_the_latest_candles(self):
        index = pd.date_range("2024-01-01", periods=200, freq="B")
        old = dict(zone("demand", 95, 97), label="Daily Demand", confluence=False, timeframes=["1d"],
                   time=int(index[10].timestamp()))
        recent = dict(old, time=int(index[190].timestamp()))
        rows = sd_zones.chart_rectangles([old, recent], index, bars=40)
        self.assertEqual(rows[0]["start_time"], int(index[160].timestamp()))
        self.assertEqual(rows[0]["time"], int(index[10].timestamp()))
        self.assertEqual(rows[1]["start_time"], int(index[190].timestamp()))
        self.assertEqual(rows[0]["end_time"], int(index[-1].timestamp()))
        self.assertEqual(rows[0]["color"], "#16a34a")
        self.assertFalse(rows[0]["band"])


class SharedEngineTests(unittest.TestCase):
    def test_live_and_backtest_use_the_same_engine(self):
        data = frame(demand_bars())
        engine = sd_zones.nearest_map(data)
        self.assertEqual(nearest_zones(data), engine)
        self.assertEqual(zones_asof(data), engine)


if __name__ == "__main__":
    unittest.main()
