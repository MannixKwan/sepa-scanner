"""Buy orders you've placed, recorded from the dashboard (data/orders.json).

Lifecycle:  PENDING -> TRIGGERED (price traded through it; fill price estimated)
                    -> FILLED    (you confirmed the actual fill)  -> CLOSED (you sold)
            PENDING -> CANCELLED (you cancelled) / EXPIRED (day order not filled)
TRIGGERED and FILLED orders are managed as positions by the exit rules; PENDING orders reserve cash
and a position slot in the allocation. The scanner checks every pending order against the latest
bars, queues an alert when one triggers, and warns when a pending order should be cancelled.
"""
from __future__ import annotations

import fcntl
import json
import re
import uuid
from contextlib import contextmanager
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd

from .config import DATA_DIR, SITE_DIR

ET = ZoneInfo("America/New_York")
PATH = DATA_DIR / "orders.json"
LOCK = DATA_DIR / "orders.lock"
TYPES = {"stop_limit": "Buy stop-limit", "stop": "Buy stop", "limit": "Buy limit", "filled": "Already bought"}
ACTIVE = ("TRIGGERED", "FILLED")       # managed as positions
WATCHED = ("PENDING",) + ACTIVE
_TICKER = re.compile(r"^[A-Z][A-Z0-9\-]{0,9}$")


# ------------------------------------------------------------------ storage
@contextmanager
def _locked():
    """Serialises read-modify-write between the scheduler, the web server and manual scans."""
    DATA_DIR.mkdir(exist_ok=True)
    with open(LOCK, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def _read() -> list[dict]:
    try:
        return json.loads(PATH.read_text()).get("orders", [])
    except Exception:
        return []


def _write(orders: list[dict]) -> None:
    tmp = PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps({"orders": orders}, indent=1))
    tmp.replace(PATH)


def load() -> list[dict]:
    with _locked():
        return _read()


def _now() -> str:
    return datetime.now(ET).isoformat(timespec="seconds")


def _price(value, name: str, required: bool = True) -> float | None:
    if value in (None, ""):
        if required:
            raise ValueError(f"{name} is required")
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number") from None
    if not 0 < v < 1e7:
        raise ValueError(f"{name} must be a positive price")
    return round(v, 4)


def _todays_high(ticker: str) -> tuple[str | None, float | None]:
    """The last bar the dashboard knows for this stock - so a high made *before* you placed the
    order can't trigger it."""
    try:
        c = json.loads((SITE_DIR / "data" / "t" / f"{ticker}.json").read_text())["chart"]
        return c["d"][-1], c["h"][-1]
    except Exception:
        return None, None


# ----------------------------------------------------------- user actions
def create(data: dict) -> dict:
    t = str(data.get("ticker", "")).strip().upper().replace(".", "-")
    if not _TICKER.match(t):
        raise ValueError("invalid ticker")
    typ = data.get("type", "stop_limit")
    if typ not in TYPES:
        raise ValueError("invalid order type")
    try:
        shares = int(float(data.get("shares") or 0))
    except (TypeError, ValueError):
        raise ValueError("shares must be a whole number") from None
    if not 0 < shares <= 10_000_000:
        raise ValueError("shares must be a positive whole number")
    o = {
        "id": uuid.uuid4().hex[:10], "ticker": t, "type": typ, "shares": shares,
        "trigger": None, "limit": None,
        "stop_loss": _price(data.get("stop_loss"), "stop loss", required=False),
        "expiry": data.get("expiry") if data.get("expiry") in ("GTC", "DAY") else "GTC",
        "notes": str(data.get("notes") or "")[:200],
        "status": "PENDING", "placed_at": _now(), "source": "manual", "outbox": [],
    }
    if typ in ("stop", "stop_limit"):
        o["trigger"] = _price(data.get("trigger"), "trigger price")
    if typ in ("stop_limit", "limit"):
        o["limit"] = _price(data.get("limit"), "limit price")
    if typ == "stop_limit" and o["limit"] < o["trigger"]:
        raise ValueError("the limit must be at or above the trigger price")
    if typ == "filled":
        o["fill_price"] = _price(data.get("fill_price"), "fill price")
        o["status"], o["filled_at"] = "FILLED", str(data.get("filled_date") or date.today().isoformat())[:10]
    if o["stop_loss"] is not None:
        ref = o.get("fill_price") or o["trigger"] or o["limit"]
        if ref and o["stop_loss"] >= ref:
            raise ValueError("the stop loss must be below the buy price")
    o["ref_date"], o["ref_high"] = _todays_high(t)
    with _locked():
        orders = _read()
        orders.append(o)
        _write(orders)
    return o


def update(order_id: str, action: str, data: dict | None = None) -> dict:
    data = data or {}
    with _locked():
        orders = _read()
        o = next((x for x in orders if x["id"] == order_id), None)
        if o is None:
            raise KeyError("order not found")
        st = o["status"]
        if action == "cancel" and st == "PENDING":
            o.update(status="CANCELLED", cancelled_at=_now())
        elif action == "confirm" and st in ACTIVE:
            o["fill_price"] = _price(data.get("fill_price"), "fill price")
            if data.get("shares"):
                o["shares"] = max(1, int(float(data["shares"])))
            o.update(status="FILLED", filled_at=o.get("triggered_at") or date.today().isoformat())
        elif action == "close" and st in ACTIVE:
            o.update(status="CLOSED", exit_price=_price(data.get("exit_price"), "exit price", required=False),
                     closed_at=_now())
        elif action == "stop" and st in WATCHED:
            o["stop_loss"] = _price(data.get("stop_loss"), "stop loss")
        elif action == "delete" and st in ("CANCELLED", "EXPIRED", "CLOSED"):
            orders.remove(o)
        else:
            raise ValueError(f"can't {action} an order that is {st.lower()}")
        _write(orders)
        return o


# ------------------------------------------------------------ monitoring
def _fill(o: dict, bar) -> float | None:
    """Estimated fill if this daily bar would have executed the order, else None."""
    s, lim = o.get("trigger"), o.get("limit")
    if o["type"] == "stop":
        return max(s, bar.open) if bar.high >= s else None
    if o["type"] == "stop_limit":
        if bar.high < s or max(bar.low, s) > lim:
            return None                       # never reached the trigger, or only traded above the limit
        return min(max(bar.open, s), lim)
    if o["type"] == "limit":
        return min(bar.open, lim) if bar.low <= lim else None
    return None


def _queue(o: dict, kind: str, icon: str, text: str) -> None:
    key = f"order:{o['id']}:{kind}"
    if all(m["key"] != key for m in o.setdefault("outbox", [])):
        o["outbox"].append({"key": key, "icon": icon, "ticker": o["ticker"], "text": text})


def process(frames: dict[str, pd.DataFrame], session_closed: bool, today: date | None = None) -> None:
    """Advance pending orders against the latest daily bars (today's bar may be partial)."""
    today = today or datetime.now(ET).date()
    with _locked():
        orders = _read()
        for o in orders:
            if o["status"] != "PENDING":
                continue
            df = frames.get(o["ticker"])
            if df is None or df.empty:
                continue
            bars = df[df.index >= pd.Timestamp(o["placed_at"][:10])]
            for d, bar in bars.iterrows():
                ds = d.strftime("%Y-%m-%d")
                if ds == o.get("ref_date") and o.get("ref_high") is not None and bar.high <= o["ref_high"]:
                    continue                  # no new high since you placed the order
                px = _fill(o, bar)
                if px is not None:
                    o.update(status="TRIGGERED", fill_price_est=round(float(px), 2), triggered_at=ds)
                    stop = f" · stop {o['stop_loss']:,.2f}" if o.get("stop_loss") else ""
                    _queue(o, "triggered", "✅", f"buy order triggered: {o['shares']:,} sh at ~{px:,.2f} "
                           f"({TYPES[o['type']].lower()}){stop}. Now tracked as a position - confirm your actual "
                           "fill price on the Portfolio page")
                    break
                if o["type"] == "stop_limit" and bar.high >= o["trigger"] and bar.low > o["limit"]:
                    _queue(o, f"above_limit:{ds}", "⚠️", f"traded above your {o['limit']:,.2f} limit without "
                           "filling - the order is still open; it fills only if the price comes back")
            if o["status"] == "PENDING" and o["expiry"] == "DAY" and len(bars):
                last = bars.index[-1].date()
                if last < today or (last == today and session_closed):
                    o.update(status="EXPIRED", expired_at=_now())
                    _queue(o, "expired", "⌛", "day order expired without filling")
        _write(orders)


def outbox() -> list[dict]:
    return [m for o in load() for m in o.get("outbox", [])]


def ack(keys: set[str]) -> None:
    """Drop delivered alerts from the outbox (undelivered ones are retried next run)."""
    if not keys:
        return
    with _locked():
        orders = _read()
        for o in orders:
            o["outbox"] = [m for m in o.get("outbox", []) if m["key"] not in keys]
        _write(orders)


# ---------------------------------------------------------------- views
def as_positions(orders: list[dict]) -> list[dict]:
    out = []
    for o in orders:
        if o["status"] not in ACTIVE:
            continue
        est = o["status"] == "TRIGGERED"
        out.append({
            "ticker": o["ticker"], "shares": float(o["shares"]),
            "entry_price": float(o.get("fill_price") or o.get("fill_price_est")),
            "entry_date": o.get("filled_at") or o.get("triggered_at"),
            "stop": o.get("stop_loss"),
            "notes": "From your buy order" + (" - fill price estimated, confirm it below" if est else ""),
            "source": "order", "order_id": o["id"], "fill_estimated": est,
        })
    return out


def as_pending_buys(orders: list[dict]) -> list[dict]:
    return [{"ticker": o["ticker"], "qty": float(o["shares"]), "type": TYPES[o["type"]],
             "price": o.get("limit") or o.get("trigger"),
             "value_usd": round(o["shares"] * (o.get("limit") or o.get("trigger") or 0), 2),
             "source": "manual", "order_id": o["id"]}
            for o in orders if o["status"] == "PENDING"]


def annotate(orders: list[dict], frames: dict[str, pd.DataFrame], rows: dict[str, dict]) -> list[dict]:
    """Orders for the dashboard/digest with the current price and warnings attached."""
    out = []
    for o in orders:
        x = {k: v for k, v in o.items() if k != "outbox"}
        df = frames.get(o["ticker"])
        if df is not None and len(df):
            x["price"] = round(float(df["close"].iloc[-1]), 2)
            ref = o.get("trigger") or o.get("limit")
            if o["status"] == "PENDING" and ref:
                x["distance"] = round(ref / x["price"] - 1, 4)
        r = rows.get(o["ticker"])
        if r and o["status"] == "PENDING":
            x["setup_status"] = r["status"]
            x["warnings"] = warnings(o, r)
        out.append(x)
    return out


def warnings(o: dict, r: dict, earnings_days: int = 7) -> list[str]:
    """Reasons a pending buy order may need cancelling or adjusting."""
    w = []
    if r["status"] == "FAILED":
        w.append("the base failed - consider cancelling")
    dte = r.get("days_to_earnings")
    if dte is not None and 0 <= dte <= earnings_days:
        w.append(f"earnings in {dte} day{'s' if dte != 1 else ''} - consider cancelling or reducing")
    piv = (r.get("plan") or {}).get("entry")
    if piv and o.get("trigger") and abs(piv / o["trigger"] - 1) > 0.02:
        w.append(f"the pivot is now {piv:,.2f} (your trigger is {o['trigger']:,.2f}) - consider updating")
    if (r.get("plan") or {}).get("too_wide"):
        w.append("the setup's stop is now wider than the maximum - not a low-risk entry")
    return w
