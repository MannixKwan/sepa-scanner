"""Indicators, IBD-style RS rating and Minervini's Trend Template."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    c = out["close"]
    out["sma50"] = c.rolling(50).mean()
    out["sma150"] = c.rolling(150).mean()
    out["sma200"] = c.rolling(200).mean()
    out["ema21"] = c.ewm(span=21, adjust=False).mean()
    out["ema10"] = c.ewm(span=10, adjust=False).mean()
    out["vol50"] = out["volume"].rolling(50).mean()
    prev = c.shift(1)
    tr = pd.concat([out["high"] - out["low"], (out["high"] - prev).abs(), (out["low"] - prev).abs()], axis=1).max(axis=1)
    out["atr14"] = tr.rolling(14).mean()
    return out


# ---------------------------------------------------------------- RS rating
def rs_raw(close: pd.Series) -> float:
    """IBD-style weighted performance: 40% last quarter, 20% each of the three before."""
    c = close.to_numpy(dtype=float)
    n = len(c)

    def roc(k: int) -> float:
        return c[-1] / c[-1 - k] - 1.0 if n > k and c[-1 - k] > 0 else math.nan

    r = [roc(63), roc(126), roc(189), roc(252)]
    w = [0.4, 0.2, 0.2, 0.2]
    pairs = [(ri, wi) for ri, wi in zip(r, w) if not math.isnan(ri)]
    if not pairs or math.isnan(r[0]):
        return math.nan
    return sum(ri * wi for ri, wi in pairs) / sum(wi for _, wi in pairs)


def rs_ratings(raw: dict[str, float]) -> dict[str, int]:
    """Percentile-rank raw scores into a 1-99 rating across the whole universe."""
    s = pd.Series(raw, dtype=float).dropna()
    if s.empty:
        return {}
    pct = s.rank(pct=True)
    return {t: int(np.clip(round(p * 99), 1, 99)) for t, p in pct.items()}


# ----------------------------------------------------------- Trend Template
def trend_template(df: pd.DataFrame, rs_rating: int | None, cfg: dict) -> dict:
    """Mark Minervini's 8-point Trend Template (Trade Like a Stock Market Wizard, ch. 5).

    `df` must already carry indicators (add_indicators). The last row is "now".
    """
    tc = cfg["trend_template"]
    need = 200 + tc["ma200_rising_days"]
    if len(df) < need or pd.isna(df["sma200"].iloc[-1]):
        return {"passed": False, "score": 0, "checks": [], "reason": f"needs {need}+ sessions of history"}

    last = df.iloc[-1]
    price, s50, s150, s200 = last["close"], last["sma50"], last["sma150"], last["sma200"]
    year = df.iloc[-252:]
    hi52, lo52 = float(year["high"].max()), float(year["low"].min())
    s200_prev = df["sma200"].iloc[-1 - tc["ma200_rising_days"]]

    # how many consecutive months has the 200-day been rising (cap 6)?
    sma200 = df["sma200"].to_numpy()
    months = 0
    for k in range(1, 7):
        a, b = len(sma200) - 1 - 21 * (k - 1), len(sma200) - 1 - 21 * k
        if b < 0 or np.isnan(sma200[b]) or not sma200[a] > sma200[b]:
            break
        months = k

    above_low = price / lo52 - 1 if lo52 > 0 else 0.0
    below_high = 1 - price / hi52 if hi52 > 0 else 1.0
    rs = rs_rating if rs_rating is not None else 0

    def f(x):
        return f"{x:,.2f}"

    checks = [
        ("Price above 150 & 200-day MA", price > s150 and price > s200,
         f"{f(price)} vs {f(s150)} / {f(s200)}"),
        ("150-day MA above 200-day MA", s150 > s200, f"{f(s150)} vs {f(s200)}"),
        ("200-day MA trending up ≥ 1 month", s200 > s200_prev,
         f"rising ~{months} month{'s' if months != 1 else ''}"),
        ("50-day MA above 150 & 200-day MA", s50 > s150 and s50 > s200, f"{f(s50)} vs {f(s150)} / {f(s200)}"),
        ("Price above 50-day MA", price > s50, f"{f(price)} vs {f(s50)}"),
        (f"≥ {tc['min_above_52w_low']:.0%} above 52-week low", above_low >= tc["min_above_52w_low"],
         f"+{above_low:.0%} (low {f(lo52)})"),
        (f"Within {tc['max_below_52w_high']:.0%} of 52-week high", below_high <= tc["max_below_52w_high"],
         f"-{below_high:.1%} (high {f(hi52)})"),
        (f"RS rating ≥ {tc['min_rs_rating']}", rs >= tc["min_rs_rating"], f"RS {rs}"),
    ]
    score = sum(1 for _, ok, _ in checks if ok)
    return {
        "passed": score == len(checks),
        "score": score,
        "checks": [{"label": l, "pass": bool(ok), "detail": d} for l, ok, d in checks],
        "values": {
            "price": float(price), "sma50": float(s50), "sma150": float(s150), "sma200": float(s200),
            "hi52": hi52, "lo52": lo52, "pct_above_low": float(above_low), "pct_below_high": float(below_high),
            "sma200_rising_months": months,
            "ext_sma50": float(price / s50 - 1),
        },
    }
