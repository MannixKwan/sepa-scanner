"""Writes the dashboard: static assets + data/summary.json + data/t/<TICKER>.json."""
from __future__ import annotations

import json
import math
import os
import shutil

import pandas as pd

from .config import SITE_DIR, STATIC_DIR


def _clean(o):
    """JSON-safe: NaN/inf -> None, numpy scalars -> python."""
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if hasattr(o, "item") and not isinstance(o, (str, bytes)):
        try:
            o = o.item()
        except (ValueError, AttributeError):
            pass
    if isinstance(o, float) and (math.isnan(o) or math.isinf(o)):
        return None
    return o


def write_json(path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(_clean(obj), separators=(",", ":")))
    os.replace(tmp, path)


def chart_payload(df: pd.DataFrame, bench: pd.DataFrame | None, bars: int = 260) -> dict:
    d = df.iloc[-bars:]

    def arr(col, nd=2):
        return [None if pd.isna(v) else round(float(v), nd) for v in d[col]]

    out = {
        "d": [x.strftime("%Y-%m-%d") for x in d.index],
        "o": arr("open"), "h": arr("high"), "l": arr("low"), "c": arr("close"),
        "v": [int(v) for v in d["volume"].fillna(0)],
        "sma50": arr("sma50"), "sma150": arr("sma150"), "sma200": arr("sma200"),
        "ema21": arr("ema21"), "vol50": arr("vol50", 0),
    }
    if bench is not None:
        b = bench["close"].reindex(d.index).ffill()
        ratio = (d["close"] / b).dropna()
        if len(ratio):
            base = ratio.iloc[0]
            rs = (ratio / base * 100).reindex(d.index)
            out["rs"] = [None if pd.isna(v) else round(float(v), 2) for v in rs]
    return out


def copy_static() -> None:
    SITE_DIR.mkdir(exist_ok=True)
    for f in STATIC_DIR.iterdir():
        if f.is_file():
            shutil.copy2(f, SITE_DIR / f.name)


def compact(r: dict) -> dict:
    m = r.get("fund") or {}
    s = r.get("setup") or {}
    tv = (r.get("tt") or {}).get("values", {})
    return {
        "ticker": r["ticker"], "name": r["name"], "industry": r.get("industry", ""), "sector": r.get("sector", ""),
        "price": r["price"], "change_pct": r["change_pct"], "rs": r["rs"], "status": r["status"],
        "score": r["score"], "pivot_dist": r.get("pivot_dist"), "tt_score": (r.get("tt") or {}).get("score"),
        "pct_below_high": tv.get("pct_below_high"), "vcp_count": s.get("count") if s.get("valid") else None,
        "final_depth": s.get("final_depth") if s.get("valid") else None,
        "eps_q_yoy": m.get("eps_q_yoy"), "sales_q_yoy": m.get("sales_q_yoy"),
        "eps_accel": m.get("eps_accel_qtrs"), "fund_score": m.get("score"), "fund_pass": r.get("fund_pass"),
        "days_to_earnings": r.get("days_to_earnings"), "rs_new_high": r.get("rs_new_high"),
        "market_cap": r.get("market_cap"),
    }


def write_ticker(row: dict, df: pd.DataFrame, bench: pd.DataFrame | None, fund: dict | None,
                 position: dict | None = None) -> None:
    detail = {
        "row": row,
        "chart": chart_payload(df, bench),
        "fundamentals": None if not fund else {
            "eps": fund.get("eps", []), "revenue": fund.get("revenue", []),
            "metrics": fund.get("metrics"), "profile": fund.get("profile"),
            "next_earnings": fund.get("next_earnings"), "fetched_at": fund.get("fetched_at"),
        },
        "position": position,
    }
    write_json(SITE_DIR / "data" / "t" / f"{row['ticker']}.json", detail)


def write_summary(summary: dict) -> None:
    copy_static()
    write_json(SITE_DIR / "data" / "summary.json", summary)
