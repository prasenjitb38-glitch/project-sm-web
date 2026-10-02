"""The newest Yahoo daily session must not be dropped or invented."""

import math
import unittest

import pandas as pd

import app as project_app


def raw_daily(last_open):
    index = pd.to_datetime(["2026-09-29 09:15", "2026-09-30 09:15", "2026-10-01 09:15"])
    return pd.DataFrame({
        "Open": [1193.8, 1182.0, last_open],
        "High": [1198.3, 1196.5, math.nan],
        "Low": [1181.8, 1181.7, math.nan],
        "Close": [1182.0, 1187.0, math.nan],
        "Volume": [24191416, 16376789, math.nan],
    }, index=index)


META = {
    "regularMarketPrice": 1167.7,
    "regularMarketTime": 1790847900,
    "regularMarketDayHigh": 1183.9,
    "regularMarketDayLow": 1160.8,
    "regularMarketVolume": 16667234,
}


class SessionBarTest(unittest.TestCase):
    def test_empty_session_row_is_filled_from_yahoo_meta(self):
        filled = project_app._complete_session_bar("RELIANCE", raw_daily(1180.1), dict(META))
        last = filled.iloc[-1]
        self.assertEqual(filled.index[-1], pd.Timestamp("2026-10-01 09:15"))
        self.assertEqual(
            [last["Open"], last["High"], last["Low"], last["Close"], last["Volume"]],
            [1180.1, 1183.9, 1160.8, 1167.7, 16667234.0],
        )
        self.assertEqual(filled.iloc[-2]["Close"], 1187.0)

    def test_missing_meta_values_leave_the_row_empty(self):
        for missing in ("regularMarketPrice", "regularMarketDayHigh", "regularMarketDayLow"):
            meta = dict(META)
            meta.pop(missing)
            frame = project_app._complete_session_bar("RELIANCE", raw_daily(1180.1), meta)
            self.assertTrue(math.isnan(frame.iloc[-1]["Close"]), missing)

    def test_complete_row_is_not_overwritten(self):
        frame = raw_daily(1180.1)
        frame.loc[frame.index[-1], ["High", "Low", "Close", "Volume"]] = [1190.0, 1170.0, 1175.0, 1000.0]
        kept = project_app._complete_session_bar("RELIANCE", frame.copy(), dict(META))
        self.assertEqual(kept.iloc[-1]["Close"], 1175.0)


if __name__ == "__main__":
    unittest.main()
