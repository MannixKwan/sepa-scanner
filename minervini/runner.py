"""Long-running scheduler: 15-minute refreshes while NASDAQ is open, full scan after the close.

Uses the official XNAS calendar (holidays, early closes). If the machine sleeps, the loop
simply catches up with the latest due job when it wakes.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import notify
from .marketclock import Market
from .pipeline import full_scan, intraday_refresh, load_scan
from .server import serve_background
from .state import State

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")


def _safe(fn, what: str, cfg: dict, state: State):
    try:
        return fn()
    except Exception as e:
        log.exception("%s failed", what)
        day = datetime.now(ET).strftime("%Y-%m-%d")
        key = f"error:{what}"
        if not state.alerted(key, day):  # tell the user once a day, don't spam
            state.mark_alert(key, day)
            state.save()
            notify.broadcast(lambda k: f"⚠️ SEPA scanner: {what} failed: {str(e)[:300]}", cfg)
        return None


def run_forever(cfg: dict) -> None:
    if cfg["dashboard"]["enabled"]:
        serve_background(cfg)
    mk = Market()
    sc = cfg["schedule"]
    interval = timedelta(minutes=int(sc["interval_minutes"]))
    delay = timedelta(minutes=int(sc["full_scan_after_close_minutes"]))
    last_slot: datetime | None = None
    log.info("scheduler started: refresh every %s while NASDAQ is open; full scan %s after close",
             interval, delay)

    while True:
        now = datetime.now(timezone.utc)
        state = State()

        # 1) full scan for the latest completed session (also catches up after downtime)
        done = (state.get("last_full_scan") or {}).get("session")
        target = mk.last_completed(now, delay)
        if (target and (done is None or done < target)) or load_scan() is None:
            ok = _safe(lambda: full_scan(cfg, notify_plan=bool(sc["evening_plan"])), "full scan", cfg, state)
            if ok is None:
                time.sleep(300)  # e.g. network down: retry in 5 minutes
            continue

        # 2) intraday refresh on the 15-minute grid
        sess = mk.session(now.astimezone(ET).date().isoformat())
        if sess:
            o, c = sess
            if o + interval <= now <= c + timedelta(minutes=5):
                k = int((min(now, c) - o) / interval)
                slot = o + k * interval
                if slot != last_slot:
                    last_slot = slot
                    mins = (min(now, c) - o).total_seconds() / 60
                    _safe(lambda: intraday_refresh(cfg, minutes_since_open=mins,
                                                   session_minutes=(c - o).total_seconds() / 60),
                          "intraday refresh", cfg, state)
                    continue
        time.sleep(20)
