"""General-market health: benchmark trend, distribution days and NASDAQ breadth."""
from __future__ import annotations

import pandas as pd

from .technicals import add_indicators


def distribution_days(df: pd.DataFrame, window: int = 25) -> list[str]:
    """IBD-style: index down >= 0.2% on higher volume than the prior session, within the
    last 25 sessions, dropping any day the index has since rallied 5% above."""
    d = df.iloc[-(window + 1):]
    out = []
    closes = d["close"].to_numpy()
    for i in range(1, len(d)):
        chg = closes[i] / closes[i - 1] - 1
        if chg <= -0.002 and d["volume"].iloc[i] > d["volume"].iloc[i - 1]:
            if closes[i:].max() < closes[i] * 1.05:
                out.append(d.index[i].strftime("%Y-%m-%d"))
    return out


def market_regime(bench: pd.DataFrame, breadth: dict | None = None, symbol: str = "QQQ") -> dict:
    df = add_indicators(bench)
    last = df.iloc[-1]
    c, s50, s200 = float(last["close"]), float(last["sma50"]), float(last["sma200"])
    s50_rising = bool(df["sma50"].iloc[-1] > df["sma50"].iloc[-6])
    s200_rising = bool(df["sma200"].iloc[-1] > df["sma200"].iloc[-22])
    dd = distribution_days(df)
    chg = float(c / df["close"].iloc[-2] - 1)

    if c > s50 and s50 > s200 and s50_rising and len(dd) <= 4:
        regime, label, color = "uptrend", "Confirmed uptrend", "good"
        guidance = "Market supports new buys. Act on proper breakouts with normal position size."
    elif c > s200 and (c > s50 or s50_rising):
        regime, label, color = "pressure", "Uptrend under pressure", "warning"
        guidance = ("Be selective: only the strongest setups, smaller size, tighter stops. "
                    f"{len(dd)} distribution days in the last 25 sessions.")
    else:
        regime, label, color = "correction", "Market in correction", "critical"
        guidance = "Protect capital: avoid new buys, raise cash and tighten stops on what you hold."

    reasons = [
        f"{symbol} {'above' if c > s50 else 'below'} 50-day MA ({s50:,.2f})",
        f"{symbol} {'above' if c > s200 else 'below'} 200-day MA ({s200:,.2f})",
        f"50-day MA {'rising' if s50_rising else 'falling'}; 200-day MA {'rising' if s200_rising else 'falling'}",
        f"{len(dd)} distribution day{'s' if len(dd) != 1 else ''} in last 25 sessions",
    ]
    out = {
        "regime": regime, "label": label, "status": color, "guidance": guidance, "reasons": reasons,
        "benchmark": {"symbol": symbol, "close": c, "change_pct": chg, "sma50": s50, "sma200": s200,
                      "pct_from_sma50": c / s50 - 1, "pct_from_sma200": c / s200 - 1,
                      "sma50_rising": s50_rising, "sma200_rising": s200_rising},
        "distribution_days": dd,
        "as_of": df.index[-1].strftime("%Y-%m-%d"),
    }
    if breadth:
        out["breadth"] = breadth
    return out


def breadth_stats(frames: dict[str, pd.DataFrame]) -> dict:
    """% of stocks above 50/200-day MA and new 52-week highs vs lows (last session)."""
    above50 = above200 = n50 = n200 = highs = lows = 0
    for df in frames.values():
        c = df["close"]
        if len(c) >= 50:
            n50 += 1
            above50 += c.iloc[-1] > c.iloc[-50:].mean()
        if len(c) >= 200:
            n200 += 1
            above200 += c.iloc[-1] > c.iloc[-200:].mean()
        if len(df) >= 252:
            yr = df.iloc[-252:]
            highs += df["high"].iloc[-1] >= yr["high"].max()
            lows += df["low"].iloc[-1] <= yr["low"].min()
    return {
        "pct_above_50": above50 / n50 if n50 else None,
        "pct_above_200": above200 / n200 if n200 else None,
        "new_highs": int(highs), "new_lows": int(lows),
    }
