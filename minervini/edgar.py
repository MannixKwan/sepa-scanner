"""SEC EDGAR XBRL client: quarterly revenue / net income / EPS for US filers."""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import date

import requests

from .config import DATA_DIR

log = logging.getLogger(__name__)

REVENUE_TAGS = [
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "SalesRevenueNet",
    "SalesRevenueGoodsNet",
    "RevenuesNetOfInterestExpense",
]
NET_INCOME_TAGS = ["NetIncomeLoss", "ProfitLoss", "NetIncomeLossAvailableToCommonStockholdersBasic"]
EPS_TAGS = ["EarningsPerShareDiluted", "EarningsPerShareBasic"]


class SecClient:
    """Polite EDGAR client (SEC asks for <= 10 requests/second and a contact User-Agent)."""

    def __init__(self, user_agent: str):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"})
        self._lock = threading.Lock()
        self._last = 0.0
        self._ciks: dict[str, int] | None = None

    def _get(self, url: str) -> requests.Response | None:
        with self._lock:
            wait = 0.13 - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
        try:
            r = self.s.get(url, timeout=20)
        except requests.RequestException as e:
            log.debug("EDGAR %s failed: %s", url, e)
            return None
        return r if r.status_code == 200 else None

    def cik(self, ticker: str) -> int | None:
        if self._ciks is None:
            cache = DATA_DIR / "sec_tickers.json"
            if cache.exists() and time.time() - cache.stat().st_mtime < 7 * 86400:
                raw = json.loads(cache.read_text())
            else:
                r = self._get("https://www.sec.gov/files/company_tickers.json")
                raw = r.json() if r is not None else (json.loads(cache.read_text()) if cache.exists() else {})
                if r is not None:
                    cache.write_text(json.dumps(raw))
            self._ciks = {v["ticker"].upper().replace(".", "-"): int(v["cik_str"]) for v in raw.values()}
        return self._ciks.get(ticker.upper())

    def concept(self, cik: int, tag: str) -> list[dict]:
        r = self._get(f"https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:010d}/us-gaap/{tag}.json")
        if r is None:
            return []
        units = r.json().get("units", {})
        for u in ("USD", "USD/shares"):
            if u in units:
                return units[u]
        return next(iter(units.values()), [])


def quarterly(facts: list[dict]) -> list[tuple[str, float]]:
    """Single-quarter values keyed by period end; Q4 derived as fiscal year minus Q1-Q3."""
    q: dict[str, tuple[str, str, float]] = {}   # end -> (start, filed, val)
    fy: dict[str, tuple[str, str, float]] = {}
    for f in facts:
        if "start" not in f or "end" not in f or f.get("val") is None:
            continue
        days = (date.fromisoformat(f["end"]) - date.fromisoformat(f["start"])).days
        target = q if 80 <= days <= 100 else fy if 350 <= days <= 380 else None
        if target is None:
            continue
        prev = target.get(f["end"])
        if prev is None or f.get("filed", "") >= prev[1]:   # latest filing wins (restatements)
            target[f["end"]] = (f["start"], f.get("filed", ""), float(f["val"]))

    out = {end: v[2] for end, v in q.items()}
    for end, (start, _, val) in fy.items():
        if end in out:
            continue
        s, e = date.fromisoformat(start), date.fromisoformat(end)
        inside = [v[2] for qe, v in q.items()
                  if (date.fromisoformat(v[0]) - s).days >= -7 and (e - date.fromisoformat(qe)).days >= 60]
        if len(inside) == 3:
            out[end] = val - sum(inside)
    return sorted(out.items())


def _first_quarterly(sec: SecClient, cik: int, tags: list[str], min_len: int = 9) -> tuple[list, str]:
    """Most up-to-date tag, back-filled from other tags for older quarters."""
    series = []
    recent = (date.today().toordinal() - 200)
    for tag in tags:
        s = quarterly(sec.concept(cik, tag))
        if s:
            series.append((tag, s))
            if len(s) >= min_len and date.fromisoformat(s[-1][0]).toordinal() >= recent:
                break  # complete and current - no need to query the remaining tags
    if not series:
        return [], ""
    series.sort(key=lambda x: x[1][-1][0], reverse=True)
    tag, best = series[0]
    merged = dict(best)
    if len(best) < min_len:
        for _, other in series[1:]:
            for end, v in other:
                if end < best[0][0]:
                    merged.setdefault(end, v)
    return sorted(merged.items()), tag


def fetch_financials(sec: SecClient, ticker: str, need_eps: bool = True) -> dict | None:
    cik = sec.cik(ticker)
    if cik is None:
        return None
    rev, rev_tag = _first_quarterly(sec, cik, REVENUE_TAGS)
    if not rev:
        return None  # foreign filer (20-F/IFRS) or no XBRL - caller falls back to Yahoo
    ni, _ = _first_quarterly(sec, cik, NET_INCOME_TAGS)
    eps, _ = _first_quarterly(sec, cik, EPS_TAGS) if need_eps else ([], "")
    return {"cik": cik, "revenue_tag": rev_tag,
            "revenue": rev[-16:], "net_income": ni[-16:], "eps_gaap": eps[-16:]}
