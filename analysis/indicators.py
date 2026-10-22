"""Transparent indicator scoring for YourAnalyst.

RSI and MACD use the standard definitions (Wilder RSI-14,
MACD 12/26/9). Every indicator yields a value, a contribution in [-1, +1] and the rule
that produced it, so the LLM can explain the score without inventing anything.

Score = 50 + 50 * mean(contributions of the selected indicators), i.e. 0..100.
It ranks stocks inside the declared universe; it is not a forecast of returns.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

INDICATORS = ("rsi", "macd", "ma_trend", "volume")

RULES = {
    "rsi": "RSI-14: <30 oversold +1; 30-50 +0.5; 50-70 0; >70 overbought -1",
    "macd": "MACD 12/26/9 histogram: >0 and rising +1; >0 falling +0.5; <0 rising 0; <0 falling -1",
    "ma_trend": "Close vs SMA50/SMA200: close>SMA50>SMA200 +1; close>SMA200 only +0.5; "
                "close<SMA50<SMA200 -1; otherwise 0",
    "volume": "Volume / 20-day average: >=1.5 on an up day +1; >=1.5 on a down day -1; otherwise 0",
}


def _num(x, digits=4):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return None
    return round(float(x), digits)


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    # No losses at all in the window -> RSI is 100 by definition.
    return out.where(~(loss == 0), 100.0)


def macd(close: pd.Series):
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    line = ema12 - ema26
    signal = line.ewm(span=9, adjust=False).mean()
    return line, signal, line - signal


def score_rsi(df: pd.DataFrame) -> dict:
    if len(df) < 30:
        return {"available": False, "reason": "needs at least 30 daily bars"}
    v = float(rsi(df["close"]).iloc[-1])
    if v < 30:
        c, why = 1.0, "oversold (below 30)"
    elif v < 50:
        c, why = 0.5, "below neutral but not oversold (30-50)"
    elif v <= 70:
        c, why = 0.0, "neutral to strong (50-70)"
    else:
        c, why = -1.0, "overbought (above 70)"
    return {"available": True, "value": {"rsi_14": _num(v, 2)}, "contribution": c, "reason": why}


def score_macd(df: pd.DataFrame) -> dict:
    if len(df) < 35:
        return {"available": False, "reason": "needs at least 35 daily bars"}
    line, signal, hist = macd(df["close"])
    h, h_prev = float(hist.iloc[-1]), float(hist.iloc[-2])
    rising = h > h_prev
    if h > 0:
        c, why = (1.0, "histogram positive and rising") if rising else (0.5, "histogram positive but falling")
    else:
        c, why = (0.0, "histogram negative but rising") if rising else (-1.0, "histogram negative and falling")
    return {"available": True,
            "value": {"macd": _num(line.iloc[-1]), "signal": _num(signal.iloc[-1]),
                      "histogram": _num(h), "histogram_prev": _num(h_prev)},
            "contribution": c, "reason": why}


def score_ma_trend(df: pd.DataFrame) -> dict:
    if len(df) < 200:
        return {"available": False, "reason": "needs at least 200 daily bars for SMA200"}
    close = df["close"]
    px, s50, s200 = float(close.iloc[-1]), float(close.rolling(50).mean().iloc[-1]), float(close.rolling(200).mean().iloc[-1])
    if px > s50 > s200:
        c, why = 1.0, "uptrend: close above SMA50 above SMA200"
    elif px < s50 < s200:
        c, why = -1.0, "downtrend: close below SMA50 below SMA200"
    elif px > s200:
        c, why = 0.5, "close above SMA200 but not a clean uptrend"
    else:
        c, why = 0.0, "mixed trend"
    return {"available": True,
            "value": {"close": _num(px, 2), "sma50": _num(s50, 2), "sma200": _num(s200, 2)},
            "contribution": c, "reason": why}


def score_volume(df: pd.DataFrame) -> dict:
    if len(df) < 21:
        return {"available": False, "reason": "needs at least 21 daily bars"}
    vol = df["volume"]
    avg20 = float(vol.iloc[-21:-1].mean())  # previous 20 sessions, excluding today
    if not avg20 > 0:
        return {"available": False, "reason": "no volume data"}
    ratio = float(vol.iloc[-1]) / avg20
    up_day = float(df["close"].iloc[-1]) >= float(df["close"].iloc[-2])
    if ratio >= 1.5:
        c, why = (1.0, "heavy volume on an up day") if up_day else (-1.0, "heavy volume on a down day")
    else:
        c, why = 0.0, "volume not unusual"
    return {"available": True,
            "value": {"volume": int(vol.iloc[-1]), "avg20": int(avg20), "ratio": _num(ratio, 2), "up_day": up_day},
            "contribution": c, "reason": why}


SCORERS = {"rsi": score_rsi, "macd": score_macd, "ma_trend": score_ma_trend, "volume": score_volume}


def analyse(df: pd.DataFrame, indicators: list[str]) -> dict:
    """df: daily bars with columns close, volume (oldest first). Returns metrics + score."""
    df = df.dropna(subset=["close"])
    results = {name: {**SCORERS[name](df), "rule": RULES[name]} for name in indicators}
    used = [r["contribution"] for r in results.values() if r["available"]]
    score = round(50 + 50 * sum(used) / len(used), 1) if used else None
    return {"indicators": results, "score": score,
            "score_rule": "50 + 50 * mean(contributions of available indicators); 0..100",
            "bars": len(df)}
