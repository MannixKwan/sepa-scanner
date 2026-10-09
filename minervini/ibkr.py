"""Read-only Interactive Brokers sync via TWS / IB Gateway (ib_async).

Reads the account (net liquidation, cash, buying power - converted to USD when the base
currency is e.g. HKD), US stock positions with average cost, open SELL stop orders (used as
each position's current stop), pending BUY orders (cash they will consume) and other holdings.
It never places or modifies orders: the API connection is opened with readonly=True.
"""
from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)

SUMMARY_TAGS = {
    "NetLiquidation": "net_liq", "TotalCashValue": "cash", "AvailableFunds": "available_funds",
    "BuyingPower": "buying_power", "GrossPositionValue": "gross_position_value",
    "ExcessLiquidity": "excess_liquidity", "EquityWithLoanValue": "equity_with_loan",
}


def _connect(cfg: dict):
    from ib_async import IB
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    ic = cfg["ibkr"]
    ib = IB()
    ib.connect(ic["host"], int(ic["port"]), clientId=int(ic["client_id"]),
               readonly=True, timeout=10, account=ic.get("account", ""))
    return ib


def fetch_portfolio(cfg: dict) -> dict | None:
    """{"positions", "account", "other_holdings", "pending_buys", "stops"} or None if unreachable."""
    try:
        import ib_async  # noqa: F401
    except ImportError:
        log.warning("ib_async not installed; skipping IBKR sync")
        return None
    ic = cfg["ibkr"]
    try:
        ib = _connect(cfg)
    except Exception as e:
        log.warning("IBKR not reachable at %s:%s (%s) - using positions.yaml only", ic["host"], ic["port"], e)
        return None

    try:
        acct = ic.get("account") or (ib.managedAccounts() or [""])[0]

        # FX: IB quotes 'ExchangeRate' per currency as units of base currency per 1 unit of that currency
        rates = {v.currency: float(v.value) for v in ib.accountValues(acct)
                 if v.tag == "ExchangeRate" and v.currency not in ("BASE", "")}
        usd_rate = rates.get("USD", 1.0) or 1.0

        def to_usd(amount: float, cur: str) -> float:
            return amount * rates.get(cur, 1.0) / usd_rate if cur != "USD" else amount

        account = {"account": acct, "base_currency": None}
        for v in ib.accountSummary(acct):
            key = SUMMARY_TAGS.get(v.tag)
            if key is None:
                continue
            try:
                account[key] = to_usd(float(v.value), v.currency)
                account["base_currency"] = v.currency
            except ValueError:
                pass
        usd_cash = next((float(v.value) for v in ib.accountValues(acct)
                         if v.tag == "CashBalance" and v.currency == "USD"), None)
        account["usd_cash"] = usd_cash

        stops: dict[str, float] = {}
        pending = []
        for tr in ib.reqAllOpenOrders():
            c, o = tr.contract, tr.order
            if c.secType != "STK":
                continue
            if o.action == "SELL":
                px = o.auxPrice if o.orderType in ("STP", "STP LMT") else getattr(o, "trailStopPrice", None)
                if px and 0 < px < 1e9:
                    stops[c.symbol] = max(stops.get(c.symbol, 0), float(px))
            elif o.action == "BUY":
                px = next((p for p in (o.auxPrice, o.lmtPrice) if p and 0 < p < 1e9), None)
                pending.append({"ticker": c.symbol, "qty": float(o.totalQuantity), "type": o.orderType,
                                "price": px, "value_usd": to_usd(float(o.totalQuantity) * (px or 0), c.currency or "USD")})

        positions, other = [], []
        for it in ib.portfolio(acct):
            c = it.contract
            if it.position == 0:
                continue
            if c.secType == "STK" and c.currency == "USD" and it.position > 0:
                positions.append({
                    "ticker": c.symbol.replace(" ", "-").replace(".", "-"),
                    "shares": float(it.position),
                    "entry_price": float(it.averageCost),   # per share for stocks
                    "entry_date": None,
                    "stop": stops.get(c.symbol),
                    "notes": "",
                    "source": "ibkr",
                })
            else:
                other.append({"symbol": c.localSymbol or c.symbol, "type": c.secType, "currency": c.currency,
                              "qty": float(it.position), "value_usd": round(to_usd(float(it.marketValue), c.currency), 2)})

        log.info("IBKR %s: %d US stock positions, %d other holdings, %d stops, %d pending buys, NetLiq $%s",
                 acct, len(positions), len(other), len(stops), len(pending),
                 f"{account.get('net_liq', 0):,.0f}")
        return {"positions": positions, "account": account, "other_holdings": other,
                "pending_buys": pending, "stops": stops}
    finally:
        ib.disconnect()
