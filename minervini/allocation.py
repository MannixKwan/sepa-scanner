"""Portfolio allocation: Minervini-style position sizing and progressive exposure.

Principles from Trade Like a Stock Market Wizard / Think & Trade Like a Champion:
  * Size every trade off its stop: risk a small fixed slice of capital (1.25%-2.5%).
  * Cap a new position at ~25% of capital; run a concentrated book of 4-6 names
    (10-12 for large accounts). Never average down - add only to what's working.
  * Exposure follows the market AND your own results ("trade your way into the market"):
    start with half-size pilot buys, step up only when positions are working, and
    shrink back when they aren't. No new buys in a correction.

Every number here is a mechanical output of the parameters in config.yaml -> risk.
"""
from __future__ import annotations

import math

MODE_LABEL = {"defensive": "Defensive", "pilot": "Pilot", "normal": "Normal", "full": "Full"}


def mode_params(mode: str, cfg: dict) -> dict:
    rk = cfg["risk"]
    return {
        "defensive": {"risk": 0.0, "max_pos": 0.0},
        "pilot": {"risk": rk["risk_per_trade_pct"] / 2, "max_pos": rk["max_position_pct"] / 2},
        "normal": {"risk": rk["risk_per_trade_pct"], "max_pos": rk["max_position_pct"]},
        "full": {"risk": rk["full_risk_per_trade_pct"], "max_pos": rk["max_position_pct"]},
    }[mode]


def choose_mode(regime: str, positions: list[dict], cfg: dict) -> tuple[str, str]:
    """Progressive exposure: how hard to press, from the market and your own open trades."""
    rk = cfg["risk"]
    if rk.get("mode", "auto") != "auto":
        return rk["mode"], "Set manually in config.yaml (risk.mode)"
    if regime == "correction":
        return "defensive", "Market in correction: no new buys, protect what you hold"

    working = [p for p in positions if p["r_multiple"] >= 1]
    losing = [p for p in positions if p["gain_pct"] < 0]
    if not positions:
        mode, why = "pilot", "No open positions yet: start with half-size pilot buys and add once they work"
    elif not working and len(losing) * 2 >= len(positions):
        mode, why = "pilot", f"{len(losing)} of {len(positions)} positions under water: stay small until trades start working"
    elif len(working) >= 2 and not losing and any(p["r_multiple"] >= 2 for p in positions):
        mode, why = "full", f"{len(working)} positions up ≥1R and none losing: you're in sync with the market"
    else:
        mode, why = "normal", f"{len(working)} of {len(positions)} positions working (≥ +1R)"

    if regime == "pressure" and mode == "full":
        mode, why = "normal", why + "; market under pressure caps size at normal"
    return mode, why


def snapshot(cfg: dict, account: dict | None, positions: list[dict], regime: str,
             pending_buys: list[dict] | None = None, industries: dict[str, str] | None = None,
             other_holdings: list[dict] | None = None) -> dict:
    """Capital, exposure, cash, open risk and the budget left for new positions."""
    rk = cfg["risk"]
    ib = bool(account and account.get("net_liq"))
    if rk["capital_source"] == "ibkr" and ib:
        capital, source = float(account["net_liq"]), "IBKR net liquidation"
    else:
        capital, source = float(rk["account_equity"]), "config.yaml (risk.account_equity)"

    invested = sum(p["price"] * p["shares"] for p in positions)
    reserved = sum(b.get("value_usd") or 0 for b in pending_buys or [])
    if ib:
        cash = float(account.get("cash") or 0)
        deployable = float(account.get("buying_power") or 0) if rk["use_margin"] else max(0.0, cash)
    else:
        cash = max(0.0, capital - invested)
        deployable = cash
    deployable = max(0.0, deployable - reserved)

    mode, why = choose_mode(regime, positions, cfg)
    mp = mode_params(mode, cfg)
    target = min(rk["exposure_by_market"].get(regime, 0.0), rk["exposure_by_mode"].get(mode, 1.0))
    room_exposure = max(0.0, target * capital - invested - reserved)
    open_risk = sum(max(0.0, p["price"] - p["stop"]) * p["shares"] for p in positions)
    capital_at_risk = sum(max(0.0, p["entry"] - p["stop"]) * p["shares"] for p in positions)

    conc: dict[str, float] = {}
    for p in positions:
        ind = (industries or {}).get(p["ticker"]) or "Other"
        conc[ind] = conc.get(ind, 0.0) + p["price"] * p["shares"] / capital if capital else 0.0

    return {
        "capital": round(capital, 2), "capital_source": source, "ibkr": ib,
        "base_currency": (account or {}).get("base_currency"),
        "invested": round(invested, 2), "exposure_pct": invested / capital if capital else 0.0,
        "target_exposure_pct": target, "cash": round(cash, 2), "usd_cash": (account or {}).get("usd_cash"),
        "buying_power": (account or {}).get("buying_power"), "reserved_for_orders": round(reserved, 2),
        "deployable": round(deployable, 2), "budget": round(min(room_exposure, deployable), 2),
        "open_risk": round(open_risk, 2), "open_risk_pct": open_risk / capital if capital else 0.0,
        "capital_at_risk_pct": capital_at_risk / capital if capital else 0.0,
        "max_open_risk_pct": rk["max_open_risk_pct"],
        "positions": len(positions), "max_positions": rk["max_positions"],
        "slots": max(0, rk["max_positions"] - len(positions) - len(pending_buys or [])),
        "largest_position_pct": max((p["price"] * p["shares"] / capital for p in positions), default=0.0) if capital else 0.0,
        "mode": mode, "mode_label": MODE_LABEL[mode], "mode_reason": why,
        "risk_per_trade_pct": mp["risk"], "max_position_pct": mp["max_pos"],
        "concentration": sorted(({"industry": k, "pct": v} for k, v in conc.items()), key=lambda x: -x["pct"]),
        "other_holdings": other_holdings or [], "pending_buys": pending_buys or [],
        "use_margin": rk["use_margin"],
    }


def size_position(entry: float, stop: float, snap: dict) -> dict:
    """Shares for the next buy, and which limit binds."""
    capital = snap["capital"]
    risk_ps = max(entry - stop, 0.01)
    if snap["mode"] == "defensive":
        return {"shares": 0, "value": 0.0, "pct_capital": 0.0, "risk_amount": 0.0, "risk_pct_capital": 0.0,
                "limit": "market in correction - no new buys"}
    if snap["slots"] <= 0:
        return {"shares": 0, "value": 0.0, "pct_capital": 0.0, "risk_amount": 0.0, "risk_pct_capital": 0.0,
                "limit": f"already at {snap['max_positions']} positions - sell the weakest first"}
    caps = {
        f"{snap['risk_per_trade_pct']:.2%} risk per trade": capital * snap["risk_per_trade_pct"] / risk_ps,
        f"{snap['max_position_pct']:.1%} max position": capital * snap["max_position_pct"] / entry,
        "cash / exposure budget": snap["budget"] / entry,
        "portfolio open-risk cap": max(0.0, capital * snap["max_open_risk_pct"] - snap["open_risk"]) / risk_ps,
    }
    limit = min(caps, key=caps.get)
    shares = max(0, math.floor(caps[limit]))
    intended = min(list(caps.values())[:2])          # what risk & position cap alone would allow
    if shares < 0.3 * intended:                     # a sliver isn't worth taking
        shares, limit = 0, f"{limit} - too little room left for a proper position"
    value = shares * entry
    return {
        "shares": shares, "value": round(value, 2), "pct_capital": value / capital if capital else 0.0,
        "risk_amount": round(shares * risk_ps, 2), "risk_pct_capital": shares * risk_ps / capital if capital else 0.0,
        "limit": limit,
    }


def buy_price(row: dict) -> float:
    """Price you'd actually pay: the pivot, or the current price once it has broken out."""
    p = row["plan"]
    return max(row["price"], p["entry"]) if row["status"] in ("BREAKOUT", "BUY ZONE") else p["entry"]


def apply_sizing(rows: list[dict], snap: dict) -> None:
    """Replace the generic share count in each trade plan with a portfolio-aware one."""
    for r in rows:
        p = r.get("plan")
        if not p:
            continue
        z = size_position(buy_price(r), p["stop"], snap)
        p["sizing"] = z
        p["shares"], p["position_value"], p["risk_amount"] = z["shares"], z["value"], z["risk_amount"]


def allocation_plan(candidates: list[dict], snap: dict, skip: set[str] | None = None) -> dict:
    """If the best setups trigger, which to buy and how much - consuming budget, slots and risk in order.
    Stocks you already hold or have a buy order on are skipped (their cash is already counted)."""
    s = dict(snap)
    rows, total_value, total_risk = [], 0.0, 0.0
    for r in candidates:
        if not r.get("plan") or r["ticker"] in (skip or set()):
            continue
        entry = buy_price(r)
        z = size_position(entry, r["plan"]["stop"], s)
        rows.append({"rank": len(rows) + 1, "ticker": r["ticker"], "name": r["name"], "status": r["status"], "entry": round(entry, 2),
                     "stop": r["plan"]["stop"], "score": r["score"], **z})
        if z["shares"] > 0:
            total_value += z["value"]
            total_risk += z["risk_amount"]
            s["budget"] = max(0.0, s["budget"] - z["value"])
            s["slots"] -= 1
            s["open_risk"] += z["risk_amount"]
    cap = snap["capital"] or 1
    return {
        "rows": rows,
        "total_value": round(total_value, 2),
        "total_risk": round(total_risk, 2),
        "exposure_after_pct": (snap["invested"] + snap["reserved_for_orders"] + total_value) / cap,
        "open_risk_after_pct": (snap["open_risk"] + total_risk) / cap,
    }
