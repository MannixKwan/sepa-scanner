"""Tradable universe: NASDAQ-listed common stocks (optionally NYSE) from nasdaqtrader.com."""
from __future__ import annotations

import io
import logging
import re
import time

import pandas as pd
import requests

from .config import DATA_DIR

log = logging.getLogger(__name__)

NASDAQ_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"

# Securities that are not operating-company common stock.
_EXCLUDE = re.compile(
    r"warrant|\bunits?\b|\brights?\b|preferred|preference|\bnotes?\b|debenture|"
    r"\bbonds?\b|%|subordinated|acquisition corp|blank check|\bETN\b|exchange[- ]traded|"
    r"\bfund\b|\btrust units\b",
    re.I,
)


def _read_pipe(url: str) -> pd.DataFrame:
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    lines = [ln for ln in r.text.splitlines() if not ln.startswith("File Creation Time")]
    return pd.read_csv(io.StringIO("\n".join(lines)), sep="|", dtype=str, keep_default_na=False)


def load_universe(cfg: dict, max_age_hours: float = 20) -> pd.DataFrame:
    """Return DataFrame[symbol, name, exchange] of common stocks. Cached on disk."""
    cache = DATA_DIR / "universe.csv"
    if cache.exists() and time.time() - cache.stat().st_mtime < max_age_hours * 3600:
        return pd.read_csv(cache, dtype=str, keep_default_na=False)

    try:
        nq = _read_pipe(NASDAQ_URL)
        nq = nq[(nq["ETF"] == "N") & (nq["Test Issue"] == "N") & (nq["Financial Status"] == "N")]
        out = pd.DataFrame({"symbol": nq["Symbol"], "name": nq["Security Name"], "exchange": "NASDAQ"})

        if cfg["universe"].get("include_nyse"):
            ot = _read_pipe(OTHER_URL)
            ot = ot[(ot["ETF"] == "N") & (ot["Test Issue"] == "N") & (ot["Exchange"].isin(["N", "A"]))]
            out = pd.concat([out, pd.DataFrame({
                "symbol": ot["ACT Symbol"], "name": ot["Security Name"],
                "exchange": ot["Exchange"].map({"N": "NYSE", "A": "NYSE American"}),
            })])

        out = out[~out["name"].str.contains(_EXCLUDE)]
        out = out[~out["symbol"].str.contains(r"[\$\^]", regex=True)]
        out["symbol"] = out["symbol"].str.replace(".", "-", regex=False)  # Yahoo style (BRK-B)
        out = out.drop_duplicates("symbol").sort_values("symbol").reset_index(drop=True)
        out.to_csv(cache, index=False)
        log.info("Universe: %d common stocks", len(out))
        return out
    except Exception as e:  # network trouble: fall back to the last cached copy
        if cache.exists():
            log.warning("Universe download failed (%s); using cached copy", e)
            return pd.read_csv(cache, dtype=str, keep_default_na=False)
        raise
