"""Configuration loading: defaults <- config.yaml <- environment (.env)."""
from __future__ import annotations

import copy
import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
SITE_DIR = ROOT / "site"
STATIC_DIR = Path(__file__).resolve().parent / "dashboard"

DEFAULTS: dict = {
    "universe": {
        "include_nyse": False,          # NASDAQ only by default (nasdaqlisted.txt)
        "min_price": 10.0,              # Minervini avoids low-priced stocks
        "min_avg_volume": 200_000,      # 50-day average shares
        "min_avg_dollar_volume": 5_000_000,
        "extra_tickers": [],            # always analysed (e.g. NYSE names you follow)
        "benchmark": "QQQ",
    },
    "trend_template": {
        "min_above_52w_low": 0.30,      # >= 30% above 52-week low
        "max_below_52w_high": 0.25,     # within 25% of 52-week high
        "min_rs_rating": 70,
        "ma200_rising_days": 22,        # 200-day MA up for at least ~1 month
    },
    "vcp": {
        "min_contractions": 2,
        "max_contractions": 6,
        "max_base_depth": 0.40,         # first (deepest) contraction
        "max_final_depth": 0.12,        # last contraction must be tight
        "min_contraction_depth": 0.02,  # ignore wiggles smaller than this
        "min_base_days": 15,            # ~3 weeks
        "min_final_contraction_bars": 3,  # the last pullback started >= 3 sessions ago...
        "min_final_pullback_bars": 2,     # ...and took >= 2 sessions to reach its low (no one-day dips)
        "max_pivot_below_base_high": 0.10,  # pivot must be near the top of the base
        "max_base_days": 260,           # ~1 year
        "ready_within": 0.05,           # "READY" when within 5% below pivot
        "volume_dryup_ratio": 0.85,     # last contraction volume vs 50-day average
        "breakout_lookback_days": 5,    # recognise breakouts from the last N sessions
    },
    "trade": {
        "buy_zone_pct": 0.05,           # never chase > 5% above the pivot
        "max_stop_pct": 0.08,           # cap stop at 8% below entry
        "breakout_volume_ratio": 1.4,   # breakout volume >= 140% of 50-day average
    },
    "risk": {                           # position sizing & progressive exposure (allocation.py)
        "capital_source": "ibkr",       # ibkr = IBKR net liquidation; fixed = account_equity below
        "account_equity": 100_000,      # USD; used when capital_source is fixed or IBKR is unreachable
        "risk_per_trade_pct": 0.0125,   # "normal" mode: risk 1.25% of capital per trade
        "full_risk_per_trade_pct": 0.02,  # "full" mode when your trades are working (Minervini: max 2.5%)
        "max_position_pct": 0.25,       # new position <= 25% of capital (pilot mode: half)
        "max_positions": 6,             # concentrated book: 4-6 names (10-12 for large accounts)
        "max_open_risk_pct": 0.10,      # all open positions together may lose <= 10% if every stop hits
        "use_margin": False,
        "mode": "auto",                 # auto | pilot | normal | full | defensive
        "exposure_by_market": {"uptrend": 1.0, "pressure": 0.5, "correction": 0.0},
        "exposure_by_mode": {"defensive": 0.0, "pilot": 0.5, "normal": 1.0, "full": 1.0},
    },
    "fundamentals": {
        "min_eps_growth": 0.25,         # latest quarter EPS YoY
        "min_sales_growth": 0.20,       # latest quarter sales YoY (or accelerating)
        "require_for_top_picks": True,
        "cache_days": 7,
        "max_tickers_per_scan": 450,
        "workers": 6,
    },
    "positions": {
        "source": "file",               # file | ibkr | both
        "file": "positions.yaml",
        "default_stop_pct": 0.07,
        "breakeven_at_r": 2.0,          # once up 2x the initial risk, stop -> breakeven
        "lock_in_at_r": 3.0,            # once up 3R, stop -> entry + 1R
        "partial_profit_at_r": 3.0,     # suggest selling 1/3-1/2 into strength
        "partial_profit_at_pct": 0.20,
        "climax_gain_pct": 0.25,        # +25% within climax_days = climax run
        "climax_days": 15,
        "near_stop_pct": 0.03,
        "earnings_warning_days": 7,
    },
    "ibkr": {
        "host": "127.0.0.1",
        "port": 7496,                   # TWS live 7496 / paper 7497, Gateway 4001 / 4002
        "client_id": 17,
        "account": "",
    },
    "schedule": {
        "interval_minutes": 15,
        "full_scan_after_close_minutes": 45,
        "digest_times_et": ["09:45", "12:30", "15:45"],
        "send_every_run": False,        # True = full digest every 15 minutes
        "evening_plan": True,           # send tomorrow's plan after the full scan
    },
    "notifications": {
        "channels": ["telegram"],       # telegram | whatsapp_callmebot | whatsapp_twilio
        "top_n": 5,
        "local_timezone": "Asia/Hong_Kong",
    },
    "dashboard": {
        "enabled": True,
        "host": "0.0.0.0",
        "port": 8050,
        "watchlist_size": 30,
    },
}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | os.PathLike | None = None) -> dict:
    load_dotenv(ROOT / ".env")
    cfg = copy.deepcopy(DEFAULTS)
    p = Path(path) if path else ROOT / "config.yaml"
    if p.exists():
        with open(p) as f:
            cfg = _merge(cfg, yaml.safe_load(f) or {})

    env = os.environ.get
    cfg["secrets"] = {
        "telegram_bot_token": env("TELEGRAM_BOT_TOKEN", ""),
        "telegram_chat_id": env("TELEGRAM_CHAT_ID", ""),
        "callmebot_phone": env("CALLMEBOT_PHONE", ""),
        "callmebot_apikey": env("CALLMEBOT_APIKEY", ""),
        "twilio_sid": env("TWILIO_ACCOUNT_SID", ""),
        "twilio_token": env("TWILIO_AUTH_TOKEN", ""),
        "twilio_from": env("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886"),
        "twilio_to": env("TWILIO_WHATSAPP_TO", ""),
        "sec_user_agent": env("SEC_USER_AGENT", "SEPA-Scanner personal-research admin@example.com"),
        "dashboard_url": env("DASHBOARD_URL", ""),
        "dashboard_token": env("DASHBOARD_TOKEN", ""),
    }
    if env("ACCOUNT_EQUITY"):
        cfg["risk"]["account_equity"] = float(env("ACCOUNT_EQUITY"))
    # deployment overrides (e.g. docker compose on AWS points these at the ib-gateway container)
    if env("IBKR_HOST"):
        cfg["ibkr"]["host"] = env("IBKR_HOST")
    if env("IBKR_PORT"):
        cfg["ibkr"]["port"] = int(env("IBKR_PORT"))
    if env("DASHBOARD_HOST"):
        cfg["dashboard"]["host"] = env("DASHBOARD_HOST")
    DATA_DIR.mkdir(exist_ok=True)
    SITE_DIR.mkdir(exist_ok=True)
    return cfg
