"""Daily OHLCV from Yahoo Finance with an incremental on-disk cache."""
from __future__ import annotations

import logging
import pickle
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import yfinance as yf

from .config import DATA_DIR

log = logging.getLogger(__name__)

MAX_ROWS = 600          # ~2.4 years of sessions
COLS = ["open", "high", "low", "close", "volume"]


def _extract(raw: pd.DataFrame, ticker: str, single: bool) -> pd.DataFrame | None:
    try:
        if isinstance(raw.columns, pd.MultiIndex):
            if ticker not in raw.columns.get_level_values(0):
                return None
            df = raw[ticker].copy()
        elif single:
            df = raw.copy()
        else:
            return None
    except KeyError:
        return None
    df.columns = [str(c).lower() for c in df.columns]
    if not set(COLS).issubset(df.columns):
        return None
    df = df[COLS].dropna(subset=["close"])
    df = df[df["close"] > 0]
    if df.empty:
        return None
    df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
    df["volume"] = df["volume"].fillna(0)
    return df[~df.index.duplicated(keep="last")]


def download(tickers: list[str], period: str = "2y", chunk: int = 150) -> dict[str, pd.DataFrame]:
    """Batch-download daily bars. Prices are split-adjusted (not dividend-adjusted), like most charts."""
    out: dict[str, pd.DataFrame] = {}
    tickers = list(dict.fromkeys(tickers))
    for attempt in range(2):
        todo = [t for t in tickers if t not in out]
        if not todo:
            break
        for i in range(0, len(todo), chunk):
            part = todo[i:i + chunk]
            try:
                raw = yf.download(part, period=period, interval="1d", group_by="ticker",
                                  auto_adjust=False, actions=False, progress=False,
                                  threads=True, timeout=30)
            except Exception as e:
                log.warning("download chunk failed: %s", e)
                continue
            for t in part:
                df = _extract(raw, t, single=len(part) == 1)
                if df is not None:
                    out[t] = df
        if attempt == 0 and len(out) < len(tickers):
            log.info("retrying %d tickers that returned no data", len(tickers) - len(out))
    return out


def _merge(old: pd.DataFrame | None, new: pd.DataFrame) -> pd.DataFrame | None:
    """Append new bars; return None when history no longer lines up (split) so we refetch."""
    if old is None or old.empty:
        return new
    overlap = old.index.intersection(new.index)
    if len(overlap) >= 2:
        check = overlap[:-1]  # the last overlapping bar may have been a partial session
        o, n = old.loc[check, "close"], new.loc[check, "close"]
        if ((o - n).abs() / n).max() > 0.02:
            return None
    merged = pd.concat([old[~old.index.isin(new.index)], new]).sort_index()
    return merged.iloc[-MAX_ROWS:]


class PriceStore:
    def __init__(self):
        self.path = DATA_DIR / "prices_daily.pkl"
        self.data: dict[str, pd.DataFrame] = {}
        if self.path.exists():
            try:
                with open(self.path, "rb") as f:
                    self.data = pickle.load(f)
            except Exception as e:
                log.warning("price cache unreadable (%s); starting fresh", e)

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "wb") as f:
            pickle.dump(self.data, f, protocol=pickle.HIGHEST_PROTOCOL)
        tmp.replace(self.path)

    def update(self, tickers: list[str]) -> dict[str, pd.DataFrame]:
        """Bring the cache up to date for `tickers` (incremental when possible)."""
        cutoff = pd.Timestamp(datetime.now() - timedelta(days=12)).normalize()
        recent = [t for t in tickers if t in self.data and self.data[t].index[-1] >= cutoff]
        stale = [t for t in tickers if t not in recent]

        if recent:
            log.info("Incremental price update for %d tickers", len(recent))
            fresh = download(recent, period="1mo")
            for t, df in fresh.items():
                merged = _merge(self.data.get(t), df)
                if merged is None:
                    stale.append(t)  # split or adjustment -> refetch full history
                else:
                    self.data[t] = merged
        if stale:
            log.info("Full price download for %d tickers", len(stale))
            for t, df in download(stale, period="2y").items():
                self.data[t] = df.iloc[-MAX_ROWS:]
        self.save()
        return {t: self.data[t] for t in tickers if t in self.data}

    def get(self, ticker: str) -> pd.DataFrame | None:
        return self.data.get(ticker)


def with_live_bars(store: PriceStore, tickers: list[str]) -> dict[str, pd.DataFrame]:
    """Cached history + today's (partial) bar from Yahoo. Not written to the cache."""
    live = download(tickers, period="5d", chunk=100)
    out = {}
    for t in tickers:
        base = store.get(t)
        new = live.get(t)
        if new is None:
            if base is not None:
                out[t] = base
            continue
        merged = _merge(base, new) if base is not None else None
        if merged is None:  # unseen ticker or split: fetch full history once
            full = download([t], period="2y").get(t)
            if full is None:
                continue
            store.data[t] = full
            merged = _merge(full, new)
            if merged is None:
                merged = full
        out[t] = merged
    return out


# Typical cumulative share of the day's volume traded N minutes after the open (U-shaped profile).
_VOL_CURVE_MIN = np.array([0, 15, 30, 60, 90, 120, 180, 240, 300, 360, 375, 390])
_VOL_CURVE_PCT = np.array([0, .08, .13, .21, .28, .34, .45, .56, .66, .80, .88, 1.0])


def session_volume_fraction(minutes_since_open: float, session_minutes: float = 390) -> float:
    m = float(np.clip(minutes_since_open * 390.0 / max(session_minutes, 1), 0, 390))
    return float(max(np.interp(m, _VOL_CURVE_MIN, _VOL_CURVE_PCT), 0.03))


ET = ZoneInfo("America/New_York")


def intraday_volume_profile(tickers: list[str], lookback: int = 10) -> dict[str, dict]:
    """Per-stock intraday volume profile from 5-minute bars. For each ticker: `fraction` = the share
    of a full day's volume this stock has typically traded by the current clock time (average of the
    previous `lookback` sessions), and `rvol10` = today's volume so far vs those sessions' volume by
    the same time. Today's projected volume = volume so far / fraction, which is then compared with
    the 50-day average (Minervini's breakout yardstick). Returns {} on any data problem."""
    out: dict[str, dict] = {}
    tickers = list(dict.fromkeys(tickers))
    if not tickers:
        return out
    try:
        raw = yf.download(tickers, period=f"{lookback + 3}d", interval="5m", group_by="ticker",
                          auto_adjust=False, actions=False, progress=False, threads=True,
                          prepost=False, timeout=30)
    except Exception as e:
        log.warning("intraday download failed: %s", e)
        return out
    today_et = datetime.now(ET).date()
    for t in tickers:
        try:
            sub = raw[t] if isinstance(raw.columns, pd.MultiIndex) else raw
            vol = sub["Volume"].dropna()
        except KeyError:
            continue
        if vol.empty:
            continue
        idx = vol.index.tz_convert(ET) if vol.index.tz is not None else vol.index.tz_localize(ET)
        days = np.array(idx.date)
        mins = np.array(idx.hour * 60 + idx.minute) - 570          # minutes since 09:30
        v = vol.to_numpy(float)
        if days[-1] != today_et:
            continue                                                 # no bars for today yet
        cut = mins[days == today_et].max()
        today_cum = v[days == today_et].sum()
        fracs, cums = [], []
        for d in sorted(x for x in set(days) if x < today_et)[-lookback:]:
            total, by_now = v[days == d].sum(), v[(days == d) & (mins <= cut)].sum()
            if total > 0 and by_now > 0:
                fracs.append(by_now / total)
                cums.append(by_now)
        if len(fracs) < 3:
            continue
        end = 570 + cut + 5
        out[t] = {"fraction": round(float(np.mean(fracs)), 4), "rvol10": round(float(today_cum / np.mean(cums)), 2),
                  "as_of": f"{end // 60:02d}:{end % 60:02d}", "sessions": len(fracs)}
    return out
