"""Orchestration.

full_scan()        whole NASDAQ universe, once per day after the close (~3-8 min):
                   prices -> RS ratings -> Trend Template -> VCP -> fundamentals -> plans.
intraday_refresh() every 15 minutes while the market is open (~20-40 s): live prices for
                   the watchlist + your positions -> breakout / stop / exit checks -> alerts.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from . import allocation, notify, site
from .config import DATA_DIR
from .edgar import SecClient
from .fundamentals import fetch_many, get_fundamentals
from .ibkr import fetch_portfolio
from .market import breadth_stats, market_regime
from .marketclock import Market
from .positions import ACTION_ORDER, evaluate, load_file_positions, merge_positions
from .prices import PriceStore, intraday_volume_profile, session_volume_fraction, with_live_bars
from .state import State
from .strategy import STATUS_HELP, STATUS_ORDER, analyze, top_picks
from .technicals import add_indicators, rs_ratings, rs_raw, trend_template
from .universe import load_universe

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")
SCAN_FILE = DATA_DIR / "scan_latest.json"
IB_CACHE = DATA_DIR / "ibkr_last.json"      # last good IBKR snapshot, used while the gateway is down


# ----------------------------------------------------------------- helpers
def _liquid(df: pd.DataFrame, cfg: dict) -> bool:
    u = cfg["universe"]
    if len(df) < 60:
        return False
    tail = df.iloc[-50:]
    avg_vol = float(tail["volume"].mean())
    return (df["close"].iloc[-1] >= u["min_price"] and avg_vol >= u["min_avg_volume"]
            and float((tail["close"] * tail["volume"]).mean()) >= u["min_avg_dollar_volume"])


def _clean_name(n: str) -> str:
    return n.split(" - ")[0].replace(" Common Stock", "").strip()


def _load_positions(cfg: dict) -> tuple[list[dict], dict | None]:
    """Positions to manage + the raw IBKR snapshot (None when IBKR is off or unreachable)."""
    src = cfg["positions"]["source"]
    file_pos = load_file_positions(cfg)
    wants_ib = src in ("ibkr", "both") or cfg["risk"]["capital_source"] == "ibkr"
    ib = fetch_portfolio(cfg) if wants_ib else None
    if ib is not None:
        ib.update(as_of=datetime.now(ET).isoformat(timespec="seconds"), stale=False)
        site.write_json(IB_CACHE, ib)
    elif wants_ib and IB_CACHE.exists():
        # gateway down (daily restart, weekly 2FA): keep managing the last known positions
        try:
            cached = json.loads(IB_CACHE.read_text())
            if datetime.now(ET) - datetime.fromisoformat(cached["as_of"]) < timedelta(days=7):
                ib = {**cached, "stale": True}
                log.warning("IBKR unreachable - using snapshot from %s", cached["as_of"])
        except Exception as e:
            log.warning("IBKR snapshot unreadable: %s", e)
    if ib is not None and src in ("ibkr", "both"):
        pos = merge_positions(file_pos, ib["positions"], keep_file_only=(src == "both"))
    else:
        pos = file_pos
    return pos, ib


def _portfolio(cfg: dict, ib: dict | None, positions: list[dict], market: dict,
               rows: list[dict], funds: dict) -> dict:
    """Account snapshot, portfolio-aware share counts on every plan, and the allocation plan."""
    industries = {t: (f.get("profile") or {}).get("industry") for t, f in funds.items()}
    ib = ib or {}
    snap = allocation.snapshot(cfg, ib.get("account"), positions, market["regime"],
                               ib.get("pending_buys"), industries, ib.get("other_holdings"))
    allocation.apply_sizing(rows, snap)
    snap["plan"] = allocation.allocation_plan(top_picks(rows, cfg, 12), snap)
    snap["ibkr_as_of"], snap["ibkr_stale"] = ib.get("as_of"), bool(ib.get("stale"))
    return snap


def _eval_positions(pos_raw, frames, funds, cfg, state) -> list[dict]:
    out = []
    for p in pos_raw:
        df = frames.get(p["ticker"])
        if df is None or len(df) < 30:
            log.warning("no price data for position %s", p["ticker"])
            continue
        f = funds.get(p["ticker"]) or {}
        out.append(evaluate(p, add_indicators(df), cfg, f.get("next_earnings"),
                            first_seen=state.first_seen(p["ticker"])))
    out.sort(key=lambda x: (ACTION_ORDER[x["action"]], x["ticker"]))
    return out


def _watchlist(rows: list[dict], size: int) -> list[dict]:
    wl = [r for r in rows if r["status"] in ("BREAKOUT", "BUY ZONE", "READY", "FORMING")]
    wl.sort(key=lambda r: (STATUS_ORDER[r["status"]], -r["score"]))
    return wl[:size]


def _industries(rows: list[dict]) -> list[dict]:
    agg: dict[str, dict] = {}
    for r in rows:
        ind = r.get("industry")
        if not ind:
            continue
        a = agg.setdefault(ind, {"industry": ind, "sector": r.get("sector", ""), "count": 0, "rs_sum": 0, "tickers": []})
        a["count"] += 1
        a["rs_sum"] += r["rs"] or 0
        a["tickers"].append(r["ticker"])
    out = [{**a, "avg_rs": round(a["rs_sum"] / a["count"]), "tickers": sorted(a["tickers"])[:12]}
           for a in agg.values()]
    for a in out:
        a.pop("rs_sum", None)
    out.sort(key=lambda a: (-a["count"], -a["avg_rs"]))
    return out[:15]


def _summary(cfg, mode, market, rows, positions, counts, portfolio, industries) -> dict:
    now = datetime.now(ET)
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "mode": mode,
        "market": market,
        "counts": counts,
        "top_picks": top_picks(rows, cfg, cfg["notifications"]["top_n"]),
        "watchlist": _watchlist(rows, cfg["dashboard"]["watchlist_size"]),
        "tt_passers": [site.compact(r) for r in sorted(rows, key=lambda r: -r["score"])],
        "positions": positions,
        "portfolio": portfolio,
        "industries": industries,
        "status_help": STATUS_HELP,
        "settings": {
            "equity": portfolio["capital"], "ib_connected": portfolio["ibkr"],
            "risk_per_trade_pct": portfolio["risk_per_trade_pct"],
            "normal_risk_per_trade_pct": cfg["risk"]["risk_per_trade_pct"],
            "full_risk_per_trade_pct": cfg["risk"]["full_risk_per_trade_pct"],
            "max_position_pct": cfg["risk"]["max_position_pct"],
            "max_stop_pct": cfg["trade"]["max_stop_pct"], "buy_zone_pct": cfg["trade"]["buy_zone_pct"],
            "min_eps_growth": cfg["fundamentals"]["min_eps_growth"],
            "min_sales_growth": cfg["fundamentals"]["min_sales_growth"],
            "positions_rules": {k: cfg["positions"][k] for k in (
                "default_stop_pct", "breakeven_at_r", "lock_in_at_r", "partial_profit_at_r",
                "partial_profit_at_pct", "climax_gain_pct", "climax_days")},
            "local_timezone": cfg["notifications"]["local_timezone"],
            "rules": {k: cfg[k] for k in ("universe", "trend_template", "vcp", "trade", "risk", "positions",
                                          "schedule", "fundamentals", "volume")},
            "top_n": cfg["notifications"]["top_n"],
            "watchlist_size": cfg["dashboard"]["watchlist_size"],
        },
    }


def _save_scan(payload: dict) -> None:
    site.write_json(SCAN_FILE, payload)


def load_scan() -> dict | None:
    try:
        return json.loads(SCAN_FILE.read_text())
    except Exception:
        return None


# --------------------------------------------------------------- full scan
def full_scan(cfg: dict, notify_plan: bool = False) -> dict:
    t0 = time.time()
    state = State()
    uni = load_universe(cfg)
    names = {s: _clean_name(n) for s, n in zip(uni["symbol"], uni["name"])}
    pos_raw, ib = _load_positions(cfg)
    bench_sym = cfg["universe"]["benchmark"]
    pos_tickers = [p["ticker"] for p in pos_raw]
    extra = [t.upper() for t in cfg["universe"]["extra_tickers"]]
    tickers = list(dict.fromkeys(list(uni["symbol"]) + extra + pos_tickers + [bench_sym]))

    store = PriceStore()
    frames = store.update(tickers)
    bench = frames.get(bench_sym)
    if bench is None:
        raise RuntimeError(f"benchmark {bench_sym} has no data")
    log.info("prices ready for %d/%d tickers (%.0fs)", len(frames), len(tickers), time.time() - t0)

    stocks = {t: df for t, df in frames.items() if t != bench_sym}
    ratings = rs_ratings({t: rs_raw(df["close"]) for t, df in stocks.items() if len(df) > 64})
    liquid = [t for t, df in stocks.items() if _liquid(df, cfg)]
    for t in extra + pos_tickers:     # always analyse names you follow or hold
        if t in stocks and t not in liquid:
            liquid.append(t)

    ind: dict[str, pd.DataFrame] = {}
    passers = []
    for t in liquid:
        df = add_indicators(stocks[t])
        ind[t] = df
        if trend_template(df, ratings.get(t), cfg)["passed"]:
            passers.append(t)
    breadth = breadth_stats({t: stocks[t] for t in liquid})
    breadth["tt_pass"] = len(passers)
    market = market_regime(bench, breadth, bench_sym)
    log.info("%d liquid, %d pass Trend Template, market: %s", len(liquid), len(passers), market["label"])

    fc = cfg["fundamentals"]
    fund_targets = sorted(passers, key=lambda t: -(ratings.get(t) or 0))[:fc["max_tickers_per_scan"]]
    fund_targets = list(dict.fromkeys(fund_targets + [t for t in pos_tickers + extra if t in ind]))
    sec = SecClient(cfg["secrets"]["sec_user_agent"])
    funds = fetch_many(fund_targets, cfg, sec)
    log.info("fundamentals for %d tickers (%.0fs)", len(funds), time.time() - t0)

    ind_counts: dict[str, int] = {}
    for t in passers:
        i = (funds.get(t) or {}).get("profile", {}).get("industry")
        if i:
            ind_counts[i] = ind_counts.get(i, 0) + 1

    prog = Market().progress()   # scan run during market hours: today's bar is still partial
    vf = session_volume_fraction(*prog) if prog else 1.0
    basis = "projected from a typical intraday curve" if prog else "full day vs 50-day average"

    def run(t):
        r = analyze(t, ind[t], ratings.get(t), cfg, fund=funds.get(t), bench=bench, vol_fraction=vf,
                    market=market, industry_rank=ind_counts, vol_basis=basis)
        if r["name"] == t and t in names:
            r["name"] = names[t]
        return r

    rows = [run(t) for t in passers]
    extra_rows = [run(t) for t in dict.fromkeys(pos_tickers + extra) if t in ind and t not in passers]
    positions = _eval_positions(pos_raw, frames, funds, cfg, state)
    portfolio = _portfolio(cfg, ib, positions, market, rows + extra_rows, funds)

    counts = {
        "universe": len(uni), "priced": len(stocks), "liquid": len(liquid), "tt_pass": len(passers),
        "vcp": sum(1 for r in rows if r["setup"] and r["setup"]["valid"]),
        "ready": sum(1 for r in rows if r["status"] == "READY"),
        "breakouts": sum(1 for r in rows if r["status"] in ("BREAKOUT", "BUY ZONE")),
    }
    summary = _summary(cfg, "full", market, rows, positions, counts, portfolio, _industries(rows))

    pos_by_t = {p["ticker"]: p for p in positions}
    for r in rows + extra_rows:
        site.write_ticker(r, ind[r["ticker"]], bench, funds.get(r["ticker"]), pos_by_t.get(r["ticker"]))
    site.write_summary(summary)
    site.prune_tickers({r["ticker"] for r in rows + extra_rows})
    # tickers the 15-minute job re-checks today: the watchlist, top picks and fresh breakouts
    watch = [r["ticker"] for r in summary["watchlist"]] + [r["ticker"] for r in summary["top_picks"]]
    watch += [r["ticker"] for r in rows if r["status"] in ("BREAKOUT", "BUY ZONE")
              or (r["status"] == "EXTENDED" and r["setup"] and r["setup"]["valid"])]
    _save_scan({"summary": summary, "rows": rows, "extra_rows": extra_rows, "ratings": ratings,
                "watch": list(dict.fromkeys(watch)),
                "industry_counts": ind_counts, "session": bench.index[-1].strftime("%Y-%m-%d")})
    # a scan taken while the market is open doesn't complete today's session - the runner redoes it after the close
    done = Market().last_completed(datetime.now(timezone.utc), timedelta(0))
    state.set("last_full_scan", {"at": summary["generated_at"], "session": done})
    state.set("last_regime", market["regime"])
    state.save()
    log.info("full scan done in %.0fs: %s", time.time() - t0, counts)

    if notify_plan:
        notify.broadcast(lambda k: notify.build_digest(summary, cfg, k, title="SEPA plan for next session"), cfg)
    return summary


# ---------------------------------------------------------- intraday pass
def _events(prev: dict[str, dict], rows: list[dict], positions: list[dict], market: dict,
            state: State, cfg: dict, day: str, portfolio: dict | None = None) -> list[dict]:
    ev = []
    need_fund = cfg["fundamentals"]["require_for_top_picks"]

    def add(key, icon, t, text):   # marked as sent only after a successful delivery
        if not state.alerted(key, day) and all(e.get("key") != key for e in ev):
            ev.append({"key": key, "icon": icon, "ticker": t, "text": text})

    for r in rows:
        t, p = r["ticker"], r.get("plan") or {}
        if need_fund and not r.get("fund_pass"):
            continue
        if p.get("too_wide") and r["status"] != "FAILED":
            continue   # not a low-risk entry - don't alert on it
        if r["status"] == "BREAKOUT":
            # graded by volume; a later, stronger grade the same day sends an upgrade alert
            b = r.get("breakout") or {"strength": "light", "volume_ratio": r["vol_ratio"], "change": 0.0}
            rank = {"light": 0, "confirmed": 1, "power": 2}
            sent = max((rank[k] for k in rank if state.alerted(f"{t}:bo:{k}", day)), default=-1)
            if rank[b["strength"]] > sent:
                vol = f"volume {b['volume_ratio']:.1f}× normal for this time of day"
                tail = f"buy up to {p['buy_zone_hi']:,.2f} · stop {p['stop']:,.2f}"
                if b["strength"] == "power":
                    add(f"{t}:bo:power", "🔥", t,
                        f"POWER BREAKOUT{' (upgrade)' if sent >= 0 else ''}: {r['price']:,.2f} ({b['change']:+.1%} today), "
                        f"{r['pivot_dist']:+.1%} through pivot {p['entry']:,.2f} · {vol} · {tail}")
                elif b["strength"] == "confirmed":
                    add(f"{t}:bo:confirmed", "🚀", t,
                        f"breakout {'now ' if sent >= 0 else ''}confirmed by volume: {r['price']:,.2f}, "
                        f"{r['pivot_dist']:+.1%} through pivot {p['entry']:,.2f} · {vol} · {tail}")
                else:
                    add(f"{t}:bo:light", "👀", t,
                        f"crossing pivot {p['entry']:,.2f} at {r['price']:,.2f}, but {vol} so far - wait for volume "
                        "to confirm (you'll get another alert if it does)")
        elif r["status"] == "READY" and (r.get("pivot_dist") or -1) >= -0.015:
            add(f"{t}:near", "🎯", t,
                f"within {abs(r['pivot_dist']):.1%} of pivot {p['entry']:,.2f} - buy-stop above it, stop {p['stop']:,.2f}")
        elif r["status"] == "FAILED" and prev.get(t, {}).get("status") in ("BREAKOUT", "BUY ZONE"):
            add(f"{t}:failed", "❌", t, f"breakout failed - back below pivot {p.get('entry', 0):,.2f}")

    for pz in positions:
        t = pz["ticker"]
        if pz["action"] != "HOLD":
            sig = next(s for s in pz["signals"] if s["action"] == pz["action"])
            add(f"{t}:{sig['kind']}", notify.ACTION_ICON[pz["action"]], t, f"{pz['action']} - {sig['text']}")
        if pz["stop"] > pz["initial_stop"]:
            add(f"{t}:stop:{pz['stop']}", "🛡️", t, f"raise your stop to {pz['stop']:,.2f} ({pz['stop_reason']})")

    if portfolio and portfolio.get("ibkr_stale"):
        add("ibkr:offline", "🔌", "IBKR",
            f"connection down since {portfolio['ibkr_as_of'][11:16]} ET - if IBKR Mobile shows a login request, "
            "approve it. Positions shown are the last snapshot.")

    last = state.get("last_regime")
    if last and last != market["regime"]:
        ev.append({"icon": notify.REGIME_ICON.get(market["regime"], ""), "ticker": "Market",
                   "text": f"status changed to {market['label']}. {market['guidance']}"})
    state.set("last_regime", market["regime"])
    return ev


def intraday_refresh(cfg: dict, minutes_since_open: float | None = None, session_minutes: float = 390,
                     force_digest: bool = False) -> dict:
    t0 = time.time()
    scan = load_scan()
    if scan is None:
        log.info("no previous full scan - running one first")
        full_scan(cfg)
        scan = load_scan()
    state = State()
    day = datetime.now(ET).strftime("%Y-%m-%d")
    bench_sym = cfg["universe"]["benchmark"]
    pos_raw, ib = _load_positions(cfg)

    prev_rows = {r["ticker"]: r for r in scan["rows"]}
    watch = scan.get("watch") or [r["ticker"] for r in _watchlist(scan["rows"], cfg["dashboard"]["watchlist_size"])]
    pos_t = [p["ticker"] for p in pos_raw]
    tickers = list(dict.fromkeys(watch + pos_t + [bench_sym]))

    store = PriceStore()
    frames = with_live_bars(store, tickers)
    bench = frames.get(bench_sym)
    vf = 1.0 if minutes_since_open is None else session_volume_fraction(minutes_since_open, session_minutes)

    market = market_regime(bench, scan["summary"]["market"].get("breadth"), bench_sym)
    # each stock's own intraday volume profile: how much of a normal day has traded by now
    vprof = intraday_volume_profile(list(dict.fromkeys(watch + pos_t))) if minutes_since_open is not None else {}
    sec = SecClient(cfg["secrets"]["sec_user_agent"])
    funds = {t: f for t in dict.fromkeys(watch + pos_t)
             if (f := get_fundamentals(t, cfg, sec)) is not None}
    ratings, ind_counts = scan["ratings"], scan.get("industry_counts", {})

    fresh, extra_fresh = {}, []
    for t in dict.fromkeys(watch + pos_t):
        df = frames.get(t)
        if df is None or len(df) < 60:
            continue
        di = add_indicators(df)
        prof = vprof.get(t)
        if prof:
            vf_t, basis = prof["fraction"], f"time of day ({prof['as_of']} ET vs its last {prof['sessions']} sessions)"
        else:
            vf_t = vf
            basis = "projected from a typical intraday curve" if minutes_since_open is not None else "full day vs 50-day average"
        r = analyze(t, di, ratings.get(t), cfg, fund=funds.get(t), bench=bench, vol_fraction=vf_t,
                    market=market, industry_rank=ind_counts, vol_basis=basis)
        if t in prev_rows and r["name"] == t:
            r["name"] = prev_rows[t]["name"]
        if t in prev_rows or r["tt"]["passed"]:
            fresh[t] = (r, di)
        else:
            extra_fresh.append((r, di))

    rows = [fresh[t][0] if t in fresh else r for t, r in prev_rows.items()]
    rows += [fresh[t][0] for t in fresh if t not in prev_rows]
    positions = _eval_positions(pos_raw, frames, funds, cfg, state)
    portfolio = _portfolio(cfg, ib, positions, market, rows + [r for r, _ in extra_fresh], funds)

    counts = dict(scan["summary"]["counts"])
    counts["ready"] = sum(1 for r in rows if r["status"] == "READY")
    counts["breakouts"] = sum(1 for r in rows if r["status"] in ("BREAKOUT", "BUY ZONE"))
    summary = _summary(cfg, "intraday", market, rows, positions, counts, portfolio,
                       scan["summary"].get("industries", []))

    pos_by_t = {p["ticker"]: p for p in positions}
    for r, di in list(fresh.values()) + extra_fresh:
        site.write_ticker(r, di, bench, funds.get(r["ticker"]), pos_by_t.get(r["ticker"]))
    site.write_summary(summary)

    events = _events(prev_rows, [fresh[t][0] for t in fresh], positions, market, state, cfg, day, portfolio)
    scan["summary"], scan["rows"] = summary, rows
    _save_scan(scan)

    # digest at the configured ET times (or every run), alerts whenever something happens
    now_hm = datetime.now(ET).strftime("%H:%M")
    due = [s for s in cfg["schedule"]["digest_times_et"] if s <= now_hm and not state.digest_sent(s, day)]
    send_digest = force_digest or cfg["schedule"]["send_every_run"] or bool(due)

    sent = False
    if send_digest:
        def build(kind):
            msg = notify.build_digest(summary, cfg, kind)
            return (notify.build_alerts(events, cfg, kind) + "\n\n" + msg) if events else msg
        sent = notify.broadcast(build, cfg)
    elif events:
        sent = notify.broadcast(lambda k: notify.build_alerts(events, cfg, k), cfg)
    if sent:  # otherwise retry next run (e.g. Telegram not configured yet, or network down)
        for e in events:
            if e.get("key"):
                state.mark_alert(e["key"], day)
        for s in due:
            state.mark_digest(s, day)
    state.save()
    log.info("intraday refresh: %d tickers, %d events, digest=%s (%.0fs)",
             len(tickers), len(events), send_digest, time.time() - t0)
    return summary
