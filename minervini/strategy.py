"""SEPA setup analysis: Trend Template + VCP + fundamentals -> status, trade plan, score."""
from __future__ import annotations

import math
from datetime import date

import pandas as pd

from .technicals import trend_template, volume_profile
from .vcp import find_setup

# Order used to rank the watchlist.
STATUS_ORDER = {"BREAKOUT": 0, "BUY ZONE": 1, "READY": 2, "FORMING": 3, "EXTENDED": 4, "TREND": 5, "FAILED": 6}
STATUS_HELP = {
    "BREAKOUT": "Crossing the pivot today - buyable up to the top of the buy zone, ideally on strong volume.",
    "BUY ZONE": "Broke out in the last few sessions and still within 5% of the pivot.",
    "READY": "Tight VCP within 5% below the pivot - set an alert / buy-stop at the pivot.",
    "FORMING": "VCP base still developing; price more than 5% below the pivot.",
    "EXTENDED": "More than 5% above the pivot - don't chase; wait for the next base.",
    "TREND": "Passes the Trend Template but no proper base (VCP) yet.",
    "FAILED": "Undercut the last contraction low or a breakout failed to hold the pivot.",
}


def rs_line(df: pd.DataFrame, bench: pd.DataFrame | None) -> pd.Series | None:
    if bench is None:
        return None
    b = bench["close"].reindex(df.index).ffill()
    r = df["close"] / b
    return r.dropna()


def _days_until(d: str | None) -> int | None:
    if not d:
        return None
    return (date.fromisoformat(d) - date.today()).days


def trade_plan(setup: dict, price: float, cfg: dict, equity: float) -> dict:
    tr, rk = cfg["trade"], cfg["risk"]
    entry = setup["pivot"]
    stop = round(setup["stop_ref"] * 0.995, 2)          # just under the low of the last contraction
    risk = max(entry - stop, 0.01)
    # Minervini: the stop goes where the setup is wrong. If that's more than ~8% away the trade
    # isn't low-risk - skip it and wait for a tighter contraction rather than squeeze the stop.
    too_wide = risk / entry > tr["max_stop_pct"]
    shares = math.floor(equity * rk["risk_per_trade_pct"] / risk)
    shares = max(0, min(shares, math.floor(equity * rk["max_position_pct"] / entry)))
    basis = max(price, entry)
    return {
        "entry": round(entry, 2),
        "buy_zone_hi": round(entry * (1 + tr["buy_zone_pct"]), 2),
        "stop": stop,
        "too_wide": bool(too_wide),
        "risk_pct": round(risk / entry, 4),
        "risk_pct_now": round((basis - stop) / basis, 4),
        "shares": shares,
        "position_value": round(shares * entry, 2),
        "risk_amount": round(shares * risk, 2),
        "targets": {
            "2R": round(entry + 2 * risk, 2), "3R": round(entry + 3 * risk, 2),
            "+20%": round(entry * 1.20, 2), "+25%": round(entry * 1.25, 2),
        },
    }


def analyze(ticker: str, df: pd.DataFrame, rs: int | None, cfg: dict, *,
            fund: dict | None = None, bench: pd.DataFrame | None = None,
            vol_fraction: float = 1.0, equity: float | None = None,
            market: dict | None = None, industry_rank: dict | None = None) -> dict:
    """Full analysis of one ticker. `df` carries indicators; its last row is the current bar
    (possibly partial: `vol_fraction` is how much of the day's volume has normally traded)."""
    equity = equity or cfg["risk"]["account_equity"]
    tt = trend_template(df, rs, cfg)
    last = df.iloc[-1]
    price = float(last["close"])
    prev_close = float(df["close"].iloc[-2]) if len(df) > 1 else price
    vol50 = float(df["vol50"].iloc[-2]) if len(df) > 51 and not pd.isna(df["vol50"].iloc[-2]) else float(last["volume"] or 1)
    vol_ratio = float(last["volume"]) / max(vol_fraction, 0.03) / max(vol50, 1)

    setup = find_setup(df, cfg) if tt.get("score", 0) >= 7 else None
    vc, tr = cfg["vcp"], cfg["trade"]

    status, plan = "TREND", None
    if setup and setup["valid"]:
        pivot = setup["pivot"]
        plan = trade_plan(setup, price, cfg, equity)
        if price < setup["stop_ref"]:
            status = "FAILED"
        elif price > plan["buy_zone_hi"]:
            status = "EXTENDED"
        elif price >= pivot:
            status = "BREAKOUT" if setup.get("breakout_days_ago") == 0 else "BUY ZONE"
        elif setup.get("breakout_days_ago") is not None:
            # slipped back under the pivot after a breakout: a small undercut is normal, >3% is a failure
            status = "FAILED" if price < pivot * 0.97 else "READY"
        elif price >= pivot * (1 - vc["ready_within"]):
            status = "READY"
        else:
            status = "FORMING"
    elif tt.get("values", {}).get("ext_sma50", 0) > 0.25:
        status = "EXTENDED"

    # RS line (stock / benchmark): leaders' RS line makes new highs before price does.
    rsl = rs_line(df, bench)
    rs_new_high = bool(rsl is not None and len(rsl) > 60 and rsl.iloc[-1] >= rsl.iloc[-252:].max() * 0.998)
    price_new_high = bool(df["high"].iloc[-1] >= df["high"].iloc[-252:].max())

    sma50_falling = bool(len(df) > 70 and df["sma50"].iloc[-1] < df["sma50"].iloc[-22])
    vp = volume_profile(df, cfg, exclude_last=vol_fraction < 1.0)
    m = (fund or {}).get("metrics", {})
    days_to_earn = _days_until((fund or {}).get("next_earnings"))
    pivot_dist = (price / setup["pivot"] - 1) if setup else None

    # ---------------------------------------------------------------- score
    prox = {"BREAKOUT": 100, "BUY ZONE": 85, "READY": 70, "FORMING": 35, "EXTENDED": 15}.get(status, 0)
    if status == "READY" and pivot_dist is not None:
        prox = int(100 - 50 * min(abs(pivot_dist) / vc["ready_within"], 1))
    vcp_q = setup["quality"] if setup and setup["valid"] else 0
    fund_s = m.get("score", 30) if fund else 30
    vol_s = vp["score"] if vp else 50
    score = 0.20 * (rs or 0) + 0.25 * vcp_q + 0.30 * fund_s + 0.10 * vol_s + 0.15 * prox
    score += 3 if rs_new_high else 0
    score += 5 if m.get("code33") else 0
    score -= 8 if sma50_falling else 0
    base_no = setup.get("base_number") if setup and setup["valid"] else None
    late = vc["late_stage_base"]
    if base_no and base_no >= late:
        score -= min(10, 5 * (base_no - late + 1))
    if plan and plan["too_wide"]:
        score -= 10
    score = int(round(max(0, min(score, 100))))

    # ------------------------------------------------------- why / red flags
    reasons, flags = [], []
    if tt["passed"]:
        reasons.append("Passes all 8 Trend Template criteria (Stage 2 uptrend)")
    if rs:
        reasons.append(f"RS rating {rs} - outperforming {rs}% of NASDAQ stocks")
    if rs_new_high:
        reasons.append("RS line at a new high" + (" ahead of price (leadership tell)" if not price_new_high else ""))
    if setup and setup["valid"]:
        dep = " → ".join(f"{d:.0%}" for d in setup["depths"])
        reasons.append(f"{setup['count']}-contraction VCP over {setup['base_weeks']:g} weeks: {dep}")
        if base_no and base_no < late:
            reasons.append(f"{_ordinal(base_no)}-stage base - early in the advance, where the odds are best")
        if setup["vol_dryup"] <= vc["volume_dryup_ratio"]:
            reasons.append(f"Volume dried up to {setup['vol_dryup']:.0%} of average in the last contraction")
    if vp and vp["grade"] in ("A", "B"):
        reasons.append(f"Accumulation (volume grade {vp['grade']}): up/down volume {vp['ud_ratio']:.1f}, "
                       f"{vp['heavy_up']} heavy-volume up days vs {vp['heavy_down']} down in {vp['weeks']} weeks")
    if m.get("eps_q_yoy") is not None:
        acc = f", accelerating {m['eps_accel_qtrs']} qtrs" if m.get("eps_accel_qtrs", 0) >= 2 else ""
        reasons.append(f"Latest quarter EPS {m['eps_q_yoy']:+.0%} YoY{acc}")
    elif m.get("eps_q_turnaround"):
        reasons.append("EPS turned from a loss to a profit")
    if m.get("sales_q_yoy") is not None:
        acc = f", accelerating {m['sales_accel_qtrs']} qtrs" if m.get("sales_accel_qtrs", 0) >= 2 else ""
        reasons.append(f"Sales {m['sales_q_yoy']:+.0%} YoY{acc}")
    if m.get("margin_expanding_qtrs", 0) >= 2:
        reasons.append(f"Net margin expanding YoY for {m['margin_expanding_qtrs']} quarters")
    if m.get("code33"):
        reasons.append("Code 33: EPS, sales and margins all accelerating 3 quarters")
    if (m.get("est_rev_cy") or 0) >= 0.02:
        reasons.append(f"Analysts raised this year's EPS estimate {m['est_rev_cy']:+.0%} in 30 days "
                       f"({m.get('est_up_30d', 0)} up / {m.get('est_down_30d', 0)} down)")
    ind = (fund or {}).get("profile", {}).get("industry")
    if industry_rank and ind and industry_rank.get(ind, 0) >= 3:
        reasons.append(f"Industry group in leadership: {industry_rank[ind]} {ind} stocks pass the Trend Template")

    if days_to_earn is not None and 0 <= days_to_earn <= cfg["positions"]["earnings_warning_days"]:
        flags.append(f"Earnings in {days_to_earn} day{'s' if days_to_earn != 1 else ''} - avoid opening a new position right before the report")
    if sma50_falling:
        flags.append("50-day MA lower than a month ago - the uptrend is stalling")
    if vp and vp["grade"] in ("D", "E"):
        flags.append(f"Distribution (volume grade {vp['grade']}): {vp['heavy_down']} heavy-volume down days vs "
                     f"{vp['heavy_up']} up in {vp['weeks']} weeks; up/down volume {vp['ud_ratio']:.1f}")
    elif vp and vp["biggest_day"]["change"] <= -0.02 and (vp["biggest_day"]["ratio"] or 0) >= 2:
        b = vp["biggest_day"]
        flags.append(f"Heaviest volume in {vp['weeks']} weeks was a selling day ({b['date']}, "
                     f"{b['change']:+.1%} on {b['ratio']:.1f}× average)")
    if status == "EXTENDED":
        flags.append("Extended beyond the buy zone - don't chase")
    if status in ("BREAKOUT", "BUY ZONE") and setup and setup.get("breakout_days_ago") == 0 and vol_ratio < tr["breakout_volume_ratio"]:
        flags.append(f"Breakout volume light so far ({vol_ratio:.1f}× average pace; want ≥ {tr['breakout_volume_ratio']:.1f}×)")
    if plan and plan["too_wide"]:
        flags.append(f"Stop would be {plan['risk_pct']:.1%} below the pivot - over the {tr['max_stop_pct']:.0%} maximum; "
                     "not a low-risk entry until it tightens (left out of Stocks to watch)")
    if base_no and base_no >= late:
        flags.append(f"{_ordinal(base_no)}-stage base - late in the advance; late-stage bases fail more often")
    if setup and setup["valid"] and setup.get("prior_advance", 1) < vc["min_prior_advance"]:
        flags.append(f"Only {setup['prior_advance']:.0%} advance before the base (want ≥ {vc['min_prior_advance']:.0%})")
    if fund and not m.get("pass"):
        flags.append("Fundamentals below SEPA thresholds")
    flags += m.get("flags", [])
    if setup and not setup["valid"]:
        flags += setup["notes"][:2]
    if market and market.get("regime") == "correction":
        flags.append("Market in correction - new buys have poor odds")

    profile = (fund or {}).get("profile", {})
    return {
        "ticker": ticker,
        "name": profile.get("name") or ticker,
        "sector": profile.get("sector", ""),
        "industry": profile.get("industry", ""),
        "market_cap": profile.get("market_cap"),
        "inst_pct": profile.get("inst_pct"),
        "price": round(price, 2),
        "change_pct": round(price / prev_close - 1, 4),
        "volume": int(last["volume"]),
        "vol_ratio": round(vol_ratio, 2),
        "date": df.index[-1].strftime("%Y-%m-%d"),
        "rs": rs,
        "rs_new_high": rs_new_high,
        "volume_profile": vp,
        "tt": tt,
        "setup": setup,
        "status": status,
        "plan": plan,
        "pivot_dist": None if pivot_dist is None else round(pivot_dist, 4),
        "fund": m or None,
        "fund_pass": bool(m.get("pass")) if fund else None,
        "next_earnings": (fund or {}).get("next_earnings"),
        "days_to_earnings": days_to_earn,
        "score": score,
        "reasons": reasons,
        "flags": flags,
    }


def _ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def top_picks(rows: list[dict], cfg: dict, n: int) -> list[dict]:
    """The handful worth acting on: actionable status, a stop within the maximum, strong
    fundamentals, no earnings landmine."""
    need_fund = cfg["fundamentals"]["require_for_top_picks"]
    pool = [r for r in rows
            if r["status"] in ("BREAKOUT", "BUY ZONE", "READY")
            and not (r.get("plan") or {}).get("too_wide")
            and (not need_fund or r.get("fund_pass"))
            and not (r.get("days_to_earnings") is not None and 0 <= r["days_to_earnings"] <= 3)]
    pool.sort(key=lambda r: (STATUS_ORDER[r["status"]] > 1, -r["score"]))
    return pool[:n]
