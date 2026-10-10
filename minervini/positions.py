"""Open positions (positions.yaml and/or IBKR) and Minervini-style exit management.

Rules applied (all thresholds in config.yaml -> positions):
  * Initial stop: your stop, an IBKR stop order, or entry - default_stop_pct (never > 10%).
  * Once the stock has gained breakeven_at_r x the initial risk, the stop moves to breakeven
    ("never let a good gain turn into a loss").
  * Once up lock_in_at_r x risk, the stop moves to entry + 1R.
  * Sell into strength: suggest selling 1/3-1/2 at partial_profit_at_r or +partial_profit_at_pct,
    and on a climax run (+climax_gain_pct within climax_days sessions).
  * Sell into weakness: close below the 50-day MA = sell the rest; below the 21-day EMA = tighten.
  * Earnings within earnings_warning_days: hold only with a profit cushion.
  * Failed breakout: back > failed_breakout_pct below entry within failed_breakout_days = tighten.
  * Time stop: little progress after time_stop_days = consider redeploying the capital.
"""
from __future__ import annotations

import logging
import math
from datetime import date

import pandas as pd
import yaml

from .config import ROOT

log = logging.getLogger(__name__)

ACTION_ORDER = {"EXIT": 0, "SELL PARTIAL": 1, "TIGHTEN": 2, "HOLD": 3}


def load_file_positions(cfg: dict) -> list[dict]:
    path = ROOT / cfg["positions"]["file"]
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text()) or {}
    out = []
    for p in data.get("positions") or []:
        if not p.get("ticker") or not p.get("entry_price"):
            continue
        out.append({
            "ticker": str(p["ticker"]).upper().replace(".", "-"),
            "shares": float(p.get("shares") or 0),
            "entry_price": float(p["entry_price"]),
            "entry_date": str(p["entry_date"]) if p.get("entry_date") else None,
            "stop": float(p["stop"]) if p.get("stop") else None,
            "notes": p.get("notes") or "",
            "source": "file",
        })
    return out


def merge_positions(file_pos: list[dict], ib_pos: list[dict], keep_file_only: bool) -> list[dict]:
    """IBKR is the truth for shares/cost; positions.yaml adds entry date, stop and notes.
    File-only positions are kept when `keep_file_only` (source: both, or IBKR unreachable)."""
    by_t = {p["ticker"]: dict(p) for p in ib_pos}
    for p in file_pos:
        if p["ticker"] in by_t:
            ib = by_t[p["ticker"]]
            ib["entry_date"] = p["entry_date"] or ib.get("entry_date")
            ib["stop"] = p["stop"] or ib.get("stop")
            ib["notes"] = p["notes"]
            ib["source"] = "ibkr+file"
        elif keep_file_only:
            by_t[p["ticker"]] = p
    return list(by_t.values())


def evaluate(pos: dict, df: pd.DataFrame, cfg: dict, next_earnings: str | None = None,
             first_seen: str | None = None) -> dict:
    """Exit plan for one position. `df` carries indicators; last row = now."""
    pc = cfg["positions"]
    entry = pos["entry_price"]
    last = df.iloc[-1]
    price = float(last["close"])
    prev = float(df["close"].iloc[-2]) if len(df) > 1 else price

    initial_stop = pos.get("stop") or round(entry * (1 - pc["default_stop_pct"]), 2)
    if initial_stop >= entry:  # a stop already raised above cost: treat default risk as R
        r = entry * pc["default_stop_pct"]
    else:
        r = entry - initial_stop
    since_str = pos.get("entry_date") or first_seen
    since = df[df.index >= pd.Timestamp(since_str)] if since_str else df.iloc[-1:]
    if since.empty:
        since = df.iloc[-1:]
    max_close = float(max(since["close"].max(), price))
    max_gain_r = (max_close - entry) / r
    cur_r = (price - entry) / r
    gain = price / entry - 1

    stop, stop_reason = initial_stop, "initial stop"
    if max_gain_r >= pc["breakeven_at_r"] and entry > stop:
        stop, stop_reason = entry, f"breakeven (was up {pc['breakeven_at_r']:g}R)"
    if max_gain_r >= pc["lock_in_at_r"] and entry + r > stop:
        stop, stop_reason = round(entry + r, 2), f"lock in +1R (was up {pc['lock_in_at_r']:g}R)"
    stop = round(stop, 2)

    sma50, ema21 = float(last["sma50"]), float(last["ema21"])
    signals: list[tuple[str, str, str]] = []   # (action, kind, message)

    if price <= stop:
        signals.append(("EXIT", "stop_hit", f"Price {price:,.2f} is at/below the stop {stop:,.2f} ({stop_reason}) - sell"))
    if not math.isnan(sma50) and price < sma50 and len(since) >= 3:
        heavy = last["volume"] > (df["vol50"].iloc[-2] or 0)
        signals.append(("EXIT", "below_50dma", f"Below the 50-day MA ({sma50:,.2f}){' on heavy volume' if heavy else ''} - sell the rest"))
    elif not math.isnan(ema21) and price < ema21 and len(since) >= 3:
        signals.append(("TIGHTEN", "below_21ema", f"Below the 21-day EMA ({ema21:,.2f}) - tighten the stop or trim"))

    if max_gain_r >= pc["partial_profit_at_r"] or (max_close / entry - 1) >= pc["partial_profit_at_pct"]:
        signals.append(("SELL PARTIAL", "profit_target", f"Up {max_close / entry - 1:+.0%} at best ({max_gain_r:.1f}R) - "
                                        "sell 1/3-1/2 into strength, let the rest run"))
    win = df["close"].iloc[-pc["climax_days"]:]
    run = price / float(win.min()) - 1
    if run >= pc["climax_gain_pct"] and gain > 0.15:
        signals.append(("SELL PARTIAL", "climax", f"Climax run: {run:+.0%} in {pc['climax_days']} sessions - sell into strength"))

    if len(since) >= 3:
        chg = since["close"].pct_change().dropna()
        if len(chg) >= 2 and chg.iloc[-1] == chg.min() and chg.iloc[-1] < -0.03 and last["volume"] > df["vol50"].iloc[-2]:
            signals.append(("TIGHTEN", "big_down_day", f"Largest one-day drop since entry ({chg.iloc[-1]:.1%}) on heavy volume"))

    days = len(since)
    if days <= pc["failed_breakout_days"] and price > stop and price < entry * (1 - pc["failed_breakout_pct"]):
        signals.append(("TIGHTEN", "failed_breakout",
                        f"Back {1 - price / entry:.1%} below your entry after {days} sessions - the breakout is "
                        "failing; the best ones don't come back. Consider cutting before the stop"))
    if days >= pc["time_stop_days"] and gain < pc["time_stop_min_gain"] and max_gain_r < 1:
        signals.append(("HOLD", "time_stop",
                        f"{days} sessions and only {gain:+.1%} - dead money; consider freeing the capital for a better setup"))

    if price > stop and (price - stop) / price <= pc["near_stop_pct"]:
        signals.append(("TIGHTEN", "near_stop", f"Only {(price - stop) / price:.1%} above the stop {stop:,.2f}"))

    dte = (date.fromisoformat(next_earnings) - date.today()).days if next_earnings else None
    if dte is not None and 0 <= dte <= pc["earnings_warning_days"]:
        if cur_r < 1:
            signals.append(("TIGHTEN", "earnings", f"Earnings in {dte}d with little cushion ({gain:+.1%}) - consider reducing before the report"))
        else:
            signals.append(("HOLD", "earnings", f"Earnings in {dte}d - cushion of {gain:+.1%} lets you hold through"))

    action = min((a for a, _, _ in signals), key=ACTION_ORDER.get, default="HOLD")
    shares = pos.get("shares") or 0
    return {
        "ticker": pos["ticker"],
        "shares": shares,
        "entry": round(entry, 2),
        "entry_date": pos.get("entry_date"),
        "price": round(price, 2),
        "change_pct": round(price / prev - 1, 4),
        "gain_pct": round(gain, 4),
        "pnl": round((price - entry) * shares, 2),
        "r_multiple": round(cur_r, 2),
        "days_held": days,
        "max_gain_r": round(max_gain_r, 2),
        "initial_stop": round(initial_stop, 2),
        "stop": stop,
        "stop_reason": stop_reason,
        "stop_dist_pct": round((price - stop) / price, 4),
        "targets": {"2R": round(entry + 2 * r, 2), "3R": round(entry + 3 * r, 2),
                    "+20%": round(entry * 1.2, 2), "+25%": round(entry * 1.25, 2)},
        "sma50": round(sma50, 2) if not math.isnan(sma50) else None,
        "ema21": round(ema21, 2),
        "next_earnings": next_earnings,
        "days_to_earnings": dte,
        "action": action,
        "signals": [{"action": a, "kind": k, "text": t} for a, k, t in sorted(signals, key=lambda s: ACTION_ORDER[s[0]])],
        "notes": pos.get("notes", ""),
        "source": pos.get("source", "file"),
    }
