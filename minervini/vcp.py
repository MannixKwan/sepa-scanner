"""Volatility Contraction Pattern (VCP) detection.

Algorithm (alternating extremes):
  P0 = highest high of the look-back window (left side of the base)
  T1 = lowest low after P0          -> contraction 1 depth = (P0 - T1) / P0
  P1 = highest high after T1
  T2 = lowest low after P1          -> contraction 2 ...
Because every peak is the max of what follows it and every trough the min of what
follows it, highs are non-increasing and lows non-decreasing, so contractions shrink
from left to right by construction - exactly the VCP footprint. The remaining
questions are how many contractions there are, how deep the first is, how tight the
last is, and whether volume dries up. The pivot (buy point) is the high of the last
contraction; the initial stop sits under its low.

The current/last bar is excluded from the base so it can be evaluated against the pivot.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _d(ts) -> str:
    return pd.Timestamp(ts).strftime("%Y-%m-%d")


def detect_vcp(df: pd.DataFrame, cfg: dict, end_offset: int = 1) -> dict | None:
    vc = cfg["vcp"]
    n = len(df) - end_offset
    if n < 60:
        return None
    hi = df["high"].to_numpy(float)[:n]
    lo = df["low"].to_numpy(float)[:n]
    cl = df["close"].to_numpy(float)[:n]
    vol = df["volume"].to_numpy(float)[:n]
    idx = df.index[:n]

    start = max(0, n - vc["max_base_days"])
    p0 = start + int(np.argmax(hi[start:n]))
    base_days = n - 1 - p0
    if base_days < vc["min_base_days"]:
        return None  # making new highs / too young to be a base

    peaks, troughs = [p0], []
    i = p0
    while i < n - 1:
        t = i + 1 + int(np.argmin(lo[i + 1:n]))
        if (hi[i] - lo[t]) / hi[i] < vc["min_contraction_depth"]:
            break
        troughs.append(t)
        if t >= n - 1:
            break
        p = t + 1 + int(np.argmax(hi[t + 1:n]))
        peaks.append(p)
        i = p
        if len(peaks) > 12:
            break

    contractions = [(peaks[k], troughs[k]) for k in range(len(troughs))]
    # The last pullback is noise, not a contraction, if it only just started or was a one-day dip.
    while len(contractions) > 1 and (
            (n - 1 - contractions[-1][0]) < vc["min_final_contraction_bars"]
            or (contractions[-1][1] - contractions[-1][0]) < vc["min_final_pullback_bars"]):
        contractions.pop()
    if not contractions:
        return None

    depths = [(hi[p] - lo[t]) / hi[p] for p, t in contractions]
    piv_i, last_t = contractions[-1]
    pivot = float(hi[piv_i])
    stop_ref = float(lo[last_t])
    pivot_below_high = 1 - pivot / hi[p0]   # a real VCP tightens up near the top of its base

    vol50 = float(np.mean(vol[max(0, n - 50):n])) or 1.0
    last_leg = vol[piv_i:n]
    dryup = float(np.mean(last_leg[-10:]) / vol50) if len(last_leg) else 1.0
    lowest_vol_10d = bool(len(vol) >= 10 and vol[-10:].min() <= 0.5 * vol50)
    tight = float((hi[-5:].max() - lo[-5:].min()) / cl[-1])

    up = np.diff(cl[p0:n], prepend=cl[p0 - 1] if p0 > 0 else cl[p0]) > 0
    v_base = vol[p0:n]
    updown = float(v_base[up].sum() / max(v_base[~up].sum(), 1.0))

    ratios = [depths[k + 1] / depths[k] for k in range(len(depths) - 1) if depths[k] > 0]
    count = len(contractions)
    valid = (
        vc["min_contractions"] <= count <= vc["max_contractions"]
        and depths[0] <= vc["max_base_depth"]
        and depths[-1] <= vc["max_final_depth"]
        and pivot_below_high <= vc["max_pivot_below_base_high"]
    )

    # Quality 0-100: tight final contraction, volume dry-up, orderly progression, shallow base.
    q = 0
    q += 20 if 2 <= count <= 4 else (12 if count in (5, 6) else 0)
    q += 25 if depths[-1] <= 0.06 else 16 if depths[-1] <= 0.10 else 8 if depths[-1] <= 0.12 else 0
    q += 20 if dryup <= 0.6 else 12 if dryup <= 0.8 else 5 if dryup <= 1.0 else 0
    q += 15 if depths[0] <= 0.25 else 8 if depths[0] <= 0.35 else 0
    q += 10 if ratios and np.mean(ratios) <= 0.7 else (5 if ratios else 0)
    q += 10 if updown >= 1.2 else 5 if updown >= 1.0 else 0

    notes = []
    if depths[0] > vc["max_base_depth"]:
        notes.append(f"Base too deep ({depths[0]:.0%})")
    if depths[-1] > vc["max_final_depth"]:
        notes.append(f"Last contraction not tight yet ({depths[-1]:.0%})")
    if pivot_below_high > vc["max_pivot_below_base_high"]:
        notes.append(f"Pivot {pivot_below_high:.0%} below the base high - not tightening near the highs")
    if count < vc["min_contractions"]:
        notes.append("Only one contraction so far")
    if dryup <= vc["volume_dryup_ratio"]:
        notes.append(f"Volume drying up ({dryup:.0%} of 50-day avg)")
    if lowest_vol_10d:
        notes.append("Very low volume day in last 10 sessions")
    if updown >= 1.2:
        notes.append(f"Accumulation in base (up/down volume {updown:.1f})")

    return {
        "valid": bool(valid),
        "count": count,
        "depths": [round(float(x), 4) for x in depths],
        "contractions": [
            {"peak_date": _d(idx[p]), "peak": round(float(hi[p]), 2),
             "trough_date": _d(idx[t]), "trough": round(float(lo[t]), 2),
             "depth": round(float((hi[p] - lo[t]) / hi[p]), 4), "bars": int(t - p)}
            for p, t in contractions
        ],
        "pivot": round(pivot, 2),
        "pivot_date": _d(idx[piv_i]),
        "pivot_below_base_high": round(float(pivot_below_high), 4),
        "stop_ref": round(stop_ref, 2),
        "base_start": _d(idx[p0]),
        "base_days": int(base_days),
        "base_weeks": round(base_days / 5, 1),
        "base_depth": round(float(depths[0]), 4),
        "final_depth": round(float(depths[-1]), 4),
        "vol_dryup": round(dryup, 2),
        "tightness_5d": round(tight, 4),
        "updown_volume": round(updown, 2),
        "quality": int(min(q, 100)),
        "end_offset": end_offset,
        "notes": notes,
    }


def find_setup(df: pd.DataFrame, cfg: dict) -> dict | None:
    """Best VCP for the current bar, recognising breakouts from the last few sessions."""
    first = detect_vcp(df, cfg, end_offset=1)
    if first and first["valid"]:
        first["breakout_days_ago"] = 0 if df["high"].iloc[-1] > first["pivot"] else None
        return first
    for k in range(2, cfg["vcp"]["breakout_lookback_days"] + 1):
        v = detect_vcp(df, cfg, end_offset=k)
        if not v or not v["valid"]:
            continue
        after = df.iloc[-k:]
        crossed = np.flatnonzero(after["high"].to_numpy() > v["pivot"])
        if len(crossed):
            v["breakout_days_ago"] = int(k - 1 - crossed[0])
            v["breakout_date"] = _d(after.index[crossed[0]])
            return v
    if first:
        first["breakout_days_ago"] = None
    return first
