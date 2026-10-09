"""Fundamentals for SEPA: quarterly EPS / sales growth, acceleration, margins, surprises.

Sources: Yahoo earnings calendar (adjusted EPS, estimates, surprise, next report date),
Yahoo profile (sector, industry, market cap, float, institutional ownership) and SEC
EDGAR XBRL for a long, audited revenue / net-income history (Yahoo's own statement
history is only ~5 quarters). Results are cached per ticker until the next earnings
report or `cache_days`.
"""
from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta

import pandas as pd
import yfinance as yf

from .config import DATA_DIR
from .edgar import SecClient, fetch_financials

log = logging.getLogger(__name__)
CACHE = DATA_DIR / "fundamentals"


# ------------------------------------------------------------------ fetching
def _retry(fn, tries: int = 3):
    for k in range(tries):
        try:
            return fn()
        except Exception as e:  # yfinance raises assorted errors incl. rate limits
            if k == tries - 1:
                log.debug("yahoo call failed: %s", e)
                return None
            time.sleep(3 * (k + 1))


def _yahoo(ticker: str, want_statements: bool = False) -> dict:
    tk = yf.Ticker(ticker)
    info = _retry(lambda: tk.info) or {}
    profile = {
        "name": info.get("shortName") or info.get("longName") or ticker,
        "sector": info.get("sector") or "",
        "industry": info.get("industry") or "",
        "market_cap": info.get("marketCap"),
        "float_shares": info.get("floatShares"),
        "inst_pct": info.get("heldPercentInstitutions"),
        "country": info.get("country") or "",
    }

    eps_rows, next_earn = [], None
    ed = _retry(lambda: tk.get_earnings_dates(limit=16))
    if ed is not None and len(ed):
        now = pd.Timestamp.now(tz="UTC")
        for ts, row in ed.sort_index().iterrows():
            ts = pd.Timestamp(ts)
            ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts
            rep = row.get("Reported EPS")
            if pd.isna(rep):
                if ts > now and (next_earn is None or ts < next_earn):
                    next_earn = ts
                continue
            est, sur = row.get("EPS Estimate"), row.get("Surprise(%)")
            r = {"date": ts.date().isoformat(), "eps": float(rep),
                 "est": None if pd.isna(est) else float(est),
                 "surprise": None if pd.isna(sur) else float(sur)}
            if eps_rows and (date.fromisoformat(r["date"]) - date.fromisoformat(eps_rows[-1]["date"])).days < 25:
                eps_rows[-1] = r  # duplicate entry for the same quarter
            else:
                eps_rows.append(r)
    if next_earn is None:
        cal = _retry(lambda: tk.calendar) or {}
        dates = cal.get("Earnings Date") if isinstance(cal, dict) else None
        if dates:
            d0 = pd.Timestamp(dates[0])
            if d0.date() >= date.today():
                next_earn = d0

    return {"profile": profile, "eps": eps_rows,
            "next_earnings": next_earn.date().isoformat() if next_earn is not None else None,
            "statements": None}


def _yahoo_statements(ticker: str) -> dict | None:
    """Fallback for non-US filers: Yahoo's (short, ~5 quarter) income statement history."""
    qis = _retry(lambda: yf.Ticker(ticker).quarterly_income_stmt)
    if qis is None or not len(qis.columns):
        return None

    def row(name):
        if name not in qis.index:
            return []
        s = qis.loc[name].dropna()
        return sorted((pd.Timestamp(c).date().isoformat(), float(v)) for c, v in s.items())
    return {"revenue": row("Total Revenue"), "net_income": row("Net Income"), "eps_gaap": row("Diluted EPS")}


# ------------------------------------------------------------------- metrics
def _match_year_ago(dates: list[date], i: int, tol: int) -> int | None:
    best, err = None, tol + 1
    for j in range(i):
        e = abs((dates[i] - dates[j]).days - 364)
        if e < err:
            best, err = j, e
    return best if err <= tol else None


def _growth(series: list[tuple[str, float]], tol: int) -> list[dict]:
    dates = [date.fromisoformat(d) for d, _ in series]
    out = []
    for i, (d, v) in enumerate(series):
        j = _match_year_ago(dates, i, tol)
        g, turn = None, False
        if j is not None:
            prev = series[j][1]
            if prev > 0:
                g = v / prev - 1
            elif v > 0:
                turn = True  # loss -> profit
        out.append({"date": d, "value": v, "yoy": g, "turnaround": turn,
                    "prev": series[j][1] if j is not None else None})
    return out


def _accel(growth: list[float | None]) -> int:
    """Consecutive quarters (ending with the latest) in which YoY growth increased."""
    n = 0
    for k in range(len(growth) - 1, 0, -1):
        a, b = growth[k], growth[k - 1]
        if a is None or b is None or not a > b:
            break
        n += 1
    return n


def _label(d: str) -> str:
    return datetime.strptime(d, "%Y-%m-%d").strftime("%b '%y")


def build_metrics(raw: dict, cfg: dict) -> dict:
    fc = cfg["fundamentals"]
    fin = raw.get("sec") or raw.get("statements") or {}
    rev = _growth([tuple(x) for x in fin.get("revenue", [])], tol=20)
    ni = {d: v for d, v in fin.get("net_income", [])}
    rev_ends = [date.fromisoformat(r["date"]) for r in rev]

    for r in rev:
        n_i = ni.get(r["date"])
        r["margin"] = n_i / r["value"] if n_i is not None and r["value"] else None
    for i, r in enumerate(rev):
        j = _match_year_ago(rev_ends, i, 20)
        prev_m = rev[j]["margin"] if j is not None else None
        r["margin_yoy_up"] = None if r["margin"] is None or prev_m is None else r["margin"] > prev_m

    # EPS: Yahoo (adjusted, comparable to consensus) preferred, EDGAR GAAP as fallback.
    eps_src = "yahoo"
    eps_series = [(e["date"], e["eps"]) for e in raw.get("eps", [])]
    extra = {e["date"]: e for e in raw.get("eps", [])}
    if len(eps_series) < 5 and fin.get("eps_gaap"):
        eps_series, extra, eps_src = [tuple(x) for x in fin["eps_gaap"]], {}, "sec-gaap"
    eps = _growth(eps_series, tol=45)
    for e in eps:
        x = extra.get(e["date"], {})
        e["est"], e["surprise"] = x.get("est"), x.get("surprise")
        # label by the fiscal quarter the report covers when we know period ends
        rd = date.fromisoformat(e["date"])
        if eps_src == "yahoo":
            prior = [d for d in rev_ends if 0 < (rd - d).days <= 120]
            e["period"] = max(prior).isoformat() if prior else None
            e["label"] = _label(e["period"]) if e["period"] else _label(e["date"]) + "*"
        else:
            e["period"], e["label"] = e["date"], _label(e["date"])
    for r in rev:
        r["label"] = _label(r["date"])

    eps, rev = eps[-12:], rev[-12:]
    eg = [e["yoy"] for e in eps]
    sg = [r["yoy"] for r in rev]
    margin_up = 0
    for r in reversed(rev):
        if r.get("margin_yoy_up"):
            margin_up += 1
        else:
            break

    last_eps = eps[-1] if eps else None
    last_rev = rev[-1] if rev else None
    m = {
        "eps_source": eps_src,
        "eps_q_yoy": last_eps["yoy"] if last_eps else None,
        "eps_q_turnaround": bool(last_eps and last_eps["turnaround"]),
        "eps_q_yoy_prev": eg[-2] if len(eg) > 1 else None,
        "eps_accel_qtrs": _accel(eg),
        "eps_last": last_eps["value"] if last_eps else None,
        "sales_q_yoy": last_rev["yoy"] if last_rev else None,
        "sales_q_yoy_prev": sg[-2] if len(sg) > 1 else None,
        "sales_accel_qtrs": _accel(sg),
        "margin_last": last_rev["margin"] if last_rev else None,
        "margin_expanding_qtrs": margin_up,
        "eps_ttm_yoy": None,
        "surprise_last": last_eps.get("surprise") if last_eps else None,
        "positive_surprises_4q": sum(1 for e in eps[-4:] if (e.get("surprise") or 0) > 0),
        "data_quarters": len(eps),
    }
    if len(eps) >= 8:
        a, b = sum(e["value"] for e in eps[-4:]), sum(e["value"] for e in eps[-8:-4])
        m["eps_ttm_yoy"] = a / b - 1 if b > 0 else None
    m["code33"] = m["eps_accel_qtrs"] >= 3 and m["sales_accel_qtrs"] >= 3 and margin_up >= 3

    # Score 0-100 (earnings carry the most weight in SEPA).
    s = 0
    g = m["eps_q_yoy"]
    if m["eps_q_turnaround"]:
        s += 15
    elif g is not None:
        s += 30 if g >= 1.0 else 25 if g >= 0.5 else 18 if g >= 0.25 else 10 if g >= 0.15 else 4 if g > 0 else 0
    s += {0: 0, 1: 5, 2: 10}.get(m["eps_accel_qtrs"], 15)
    sg_ = m["sales_q_yoy"]
    if sg_ is not None:
        s += 20 if sg_ >= 0.5 else 15 if sg_ >= 0.25 else 12 if sg_ >= 0.2 else 6 if sg_ >= 0.1 else 2 if sg_ > 0 else 0
    s += {0: 0, 1: 4, 2: 7}.get(m["sales_accel_qtrs"], 10)
    s += 10 if margin_up >= 2 else 5 if margin_up == 1 else 0
    s += 5 if (m["surprise_last"] or 0) > 0 else 0
    s += 5 if m["positive_surprises_4q"] >= 3 else 0
    s += 5 if (m["eps_ttm_yoy"] or 0) >= 0.25 else 0
    m["score"] = int(min(s, 100))

    eps_ok = (g is not None and g >= fc["min_eps_growth"]) or (m["eps_q_turnaround"] and (m["eps_last"] or 0) > 0)
    sales_ok = (sg_ is not None and sg_ >= fc["min_sales_growth"]) or m["sales_accel_qtrs"] >= 2
    m["pass"] = bool(eps_ok and sales_ok)

    flags = []
    if g is not None and m["eps_q_yoy_prev"] is not None and g < m["eps_q_yoy_prev"] - 0.15:
        flags.append("EPS growth decelerating")
    if sg_ is not None and m["sales_q_yoy_prev"] is not None and sg_ < m["sales_q_yoy_prev"] - 0.10:
        flags.append("Sales growth decelerating")
    if (m["eps_last"] or 0) < 0:
        flags.append("Not yet profitable")
    if m["surprise_last"] is not None and m["surprise_last"] < 0:
        flags.append("Missed estimates last quarter")
    m["flags"] = flags

    return {
        "ticker": raw["ticker"],
        "fetched_at": raw["fetched_at"],
        "profile": raw.get("profile", {}),
        "next_earnings": raw.get("next_earnings"),
        "eps": [{k: e.get(k) for k in ("label", "date", "period", "value", "yoy", "turnaround", "est", "surprise")}
                for e in eps],
        "revenue": [{k: r.get(k) for k in ("label", "date", "value", "yoy", "margin")} for r in rev],
        "metrics": m,
    }


# --------------------------------------------------------------------- cache
def _fresh(path, cfg) -> dict | None:
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text())
    except Exception:
        return None
    fetched = datetime.fromisoformat(raw["fetched_at"])
    if datetime.now() - fetched > timedelta(days=cfg["fundamentals"]["cache_days"]):
        return None
    ne = raw.get("next_earnings")
    if ne and date.fromisoformat(ne) <= date.today() and fetched.date() <= date.fromisoformat(ne):
        return None  # a report has come out since we fetched
    return raw


def get_fundamentals(ticker: str, cfg: dict, sec: SecClient, refresh: bool = False) -> dict | None:
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"{ticker}.json"
    raw = None if refresh else _fresh(path, cfg)
    if raw is None:
        y = _yahoo(ticker, want_statements=False)
        sec_fin = None
        try:
            sec_fin = fetch_financials(sec, ticker, need_eps=len(y["eps"]) < 5)
        except Exception as e:
            log.debug("EDGAR failed for %s: %s", ticker, e)
        if not sec_fin:
            y["statements"] = _yahoo_statements(ticker)
        raw = {"ticker": ticker, "fetched_at": datetime.now().isoformat(timespec="seconds"),
               "sec": sec_fin, **y}
        path.write_text(json.dumps(raw))
    try:
        return build_metrics(raw, cfg)
    except Exception as e:
        log.warning("fundamental metrics failed for %s: %s", ticker, e)
        return None


def fetch_many(tickers: list[str], cfg: dict, sec: SecClient) -> dict[str, dict]:
    out: dict[str, dict] = {}
    workers = cfg["fundamentals"]["workers"]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(get_fundamentals, t, cfg, sec): t for t in tickers}
        for k, fut in enumerate(as_completed(futs), 1):
            t = futs[fut]
            try:
                res = fut.result()
            except Exception as e:
                log.warning("fundamentals %s failed: %s", t, e)
                res = None
            if res:
                out[t] = res
            if k % 50 == 0:
                log.info("fundamentals %d/%d", k, len(tickers))
    return out
