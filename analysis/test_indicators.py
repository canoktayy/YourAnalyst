"""Rule tests on synthetic price series:  python -m unittest test_indicators"""
import unittest

import numpy as np
import pandas as pd

from indicators import analyse, rsi, score_ma_trend, score_rsi, score_volume


def bars(close, volume=None):
    close = np.asarray(close, dtype=float)
    vol = np.full(len(close), 1_000_000.0) if volume is None else np.asarray(volume, dtype=float)
    return pd.DataFrame({"close": close, "volume": vol})


class IndicatorRules(unittest.TestCase):
    def test_rsi_extremes(self):
        self.assertEqual(score_rsi(bars(np.linspace(100, 200, 60)))["contribution"], -1.0)  # only gains
        self.assertEqual(score_rsi(bars(np.linspace(200, 100, 60)))["contribution"], 1.0)   # only losses
        self.assertAlmostEqual(float(rsi(pd.Series(np.linspace(100, 200, 60))).iloc[-1]), 100.0)

    def test_ma_trend(self):
        self.assertEqual(score_ma_trend(bars(np.linspace(50, 150, 250)))["contribution"], 1.0)
        self.assertEqual(score_ma_trend(bars(np.linspace(150, 50, 250)))["contribution"], -1.0)
        self.assertFalse(score_ma_trend(bars(np.linspace(50, 150, 120)))["available"])

    def test_volume_spike(self):
        vol = [1_000_000] * 30 + [2_000_000]
        self.assertEqual(score_volume(bars(list(range(100, 131)), vol))["contribution"], 1.0)
        self.assertEqual(score_volume(bars(list(range(131, 100, -1)), vol))["contribution"], -1.0)
        self.assertEqual(score_volume(bars(list(range(100, 131))))["contribution"], 0.0)

    def test_score_mean_and_unavailable(self):
        out = analyse(bars(np.linspace(150, 50, 60)), ["rsi", "ma_trend"])
        self.assertFalse(out["indicators"]["ma_trend"]["available"])
        self.assertEqual(out["score"], 100.0)  # only RSI counts: +1 -> 50 + 50


if __name__ == "__main__":
    unittest.main()
