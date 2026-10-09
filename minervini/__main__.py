"""Command line entry point:  python -m minervini <command>

  run               scheduler + dashboard (leave running; see deploy/ for auto-start)
  scan [--notify]   full NASDAQ scan now (after-close job)
  refresh [--digest]  one intraday refresh now (15-minute job)
  serve             dashboard only
  analyze TICKER    print the SEPA analysis for one stock
  test-notify       send a test message to the configured channels
  ibkr-check        test the read-only Interactive Brokers connection
  telegram-chat-id  show chat ids that messaged your bot (for TELEGRAM_CHAT_ID)
"""
from __future__ import annotations

import argparse
import logging
import sys
from logging.handlers import RotatingFileHandler

from .config import DATA_DIR, load_config


def _logging(verbose: bool) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    fh = RotatingFileHandler(DATA_DIR / "scanner.log", maxBytes=5_000_000, backupCount=3)
    fh.setFormatter(fmt)
    root.addHandler(sh)
    root.addHandler(fh)
    for noisy in ("yfinance", "urllib3", "peewee", "ib_async"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _analyze(cfg: dict, ticker: str) -> None:
    from .edgar import SecClient
    from .fundamentals import get_fundamentals
    from .market import market_regime
    from .prices import download
    from .strategy import analyze
    from .technicals import add_indicators, rs_raw
    from .pipeline import load_scan

    t = ticker.upper()
    bench_sym = cfg["universe"]["benchmark"]
    data = download([t, bench_sym], period="2y")
    if t not in data:
        sys.exit(f"no price data for {t}")
    scan = load_scan()
    rs = (scan or {}).get("ratings", {}).get(t)
    if rs is None:
        print(f"(RS rating needs a full scan for ranking; raw weighted performance {rs_raw(data[t]['close']):+.1%})")
    fund = get_fundamentals(t, cfg, SecClient(cfg["secrets"]["sec_user_agent"]))
    r = analyze(t, add_indicators(data[t]), rs, cfg, fund=fund, bench=data[bench_sym],
                market=market_regime(data[bench_sym]))
    print(f"\n{r['ticker']}  {r['name']}  [{r['industry']}]  {r['price']:,.2f}  status={r['status']}  score={r['score']}")
    print("\nTrend Template:")
    for c in r["tt"].get("checks", []):
        print(f"  {'✔' if c['pass'] else '✘'} {c['label']:<38} {c['detail']}")
    s = r["setup"]
    if s:
        print(f"\nVCP valid={s['valid']} contractions={s['count']} depths={[f'{d:.0%}' for d in s['depths']]} "
              f"base={s['base_weeks']}w pivot={s['pivot']} vol dry-up={s['vol_dryup']:.0%} quality={s['quality']}")
    if r["plan"]:
        p = r["plan"]
        print(f"Plan: buy > {p['entry']} (zone to {p['buy_zone_hi']}), stop {p['stop']} (-{p['risk_pct']:.1%}), "
              f"{p['shares']} shares, targets {p['targets']}")
    if r["fund"]:
        m = r["fund"]
        print(f"\nFundamentals score={m['score']} pass={m['pass']} EPS q/q YoY={m['eps_q_yoy']} "
              f"accel={m['eps_accel_qtrs']} sales YoY={m['sales_q_yoy']} accel={m['sales_accel_qtrs']} code33={m['code33']}")
    print("\nWhy:", *[f"  + {x}" for x in r["reasons"]], sep="\n")
    if r["flags"]:
        print("Watch out:", *[f"  - {x}" for x in r["flags"]], sep="\n")


def _ibkr_check(cfg: dict) -> None:
    from .ibkr import fetch_portfolio
    ic = cfg["ibkr"]
    print(f"Connecting read-only to {ic['host']}:{ic['port']} (client id {ic['client_id']})...")
    ib = fetch_portfolio(cfg)
    if ib is None:
        sys.exit(
            "\nCould not connect. Check that:\n"
            "  1. IB Gateway (or TWS) is running and you are logged in\n"
            "  2. API is enabled: Gateway -> Configure -> Settings -> API -> Settings\n"
            "     ('Enable ActiveX and Socket Clients' in TWS), 'Read-Only API' ticked\n"
            f"  3. The socket port there matches ibkr.port in config.yaml ({ic['port']}).\n"
            "     Gateway: live 4001 / paper 4002.  TWS: live 7496 / paper 7497\n"
            "  4. 127.0.0.1 is allowed (Trusted IPs) and no other app uses client id "
            f"{ic['client_id']}")
    a = ib["account"]
    cur = a.get("base_currency") or "USD"
    print(f"\nConnected to account {a.get('account')} (base currency {cur}; values below in USD)")
    for k, label in [("net_liq", "Net liquidation"), ("cash", "Total cash"), ("usd_cash", "USD cash balance"),
                     ("available_funds", "Available funds"), ("buying_power", "Buying power"),
                     ("gross_position_value", "Gross position value")]:
        if a.get(k) is not None:
            print(f"  {label:<22} ${a[k]:>14,.2f}")
    print(f"\nUS stock positions ({len(ib['positions'])}):")
    for p in ib["positions"]:
        stop = f"stop order {p['stop']:,.2f}" if p.get("stop") else "no stop order (scanner uses entry - default %)"
        print(f"  {p['ticker']:<6} {p['shares']:>8,.0f} sh @ {p['entry_price']:>10,.2f}   {stop}")
    if ib["pending_buys"]:
        print(f"\nOpen buy orders ({len(ib['pending_buys'])}):")
        for b in ib["pending_buys"]:
            print(f"  {b['ticker']:<6} {b['qty']:>8,.0f} {b['type']:<8} @ {b['price'] or 0:,.2f}  (${b['value_usd']:,.0f})")
    if ib["other_holdings"]:
        print(f"\nOther holdings, counted in capital but not managed ({len(ib['other_holdings'])}):")
        for o in ib["other_holdings"]:
            print(f"  {o['symbol']:<10} {o['type']:<5} {o['currency']}  ${o['value_usd']:,.0f}")
    print("\nRead-only: the scanner never places or changes orders.")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="minervini", description="Minervini SEPA scanner")
    ap.add_argument("-c", "--config", help="path to config.yaml")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    p = sub.add_parser("scan")
    p.add_argument("--notify", action="store_true", help="send the plan digest when done")
    p = sub.add_parser("refresh")
    p.add_argument("--digest", action="store_true", help="always send the digest")
    sub.add_parser("serve")
    p = sub.add_parser("analyze")
    p.add_argument("ticker")
    sub.add_parser("test-notify")
    sub.add_parser("ibkr-check")
    sub.add_parser("telegram-chat-id")
    a = ap.parse_args(argv)

    cfg = load_config(a.config)
    _logging(a.verbose)

    if a.cmd == "run":
        from .runner import run_forever
        run_forever(cfg)
    elif a.cmd == "scan":
        from .pipeline import full_scan
        full_scan(cfg, notify_plan=a.notify)
    elif a.cmd == "refresh":
        from .marketclock import Market
        from .pipeline import intraday_refresh
        prog = Market().progress()
        intraday_refresh(cfg, minutes_since_open=prog[0] if prog else None,
                         session_minutes=prog[1] if prog else 390, force_digest=a.digest)
    elif a.cmd == "serve":
        from .server import make_server
        srv = make_server(cfg)
        print(f"Dashboard: http://localhost:{cfg['dashboard']['port']}/"
              + (f"?k={cfg['secrets']['dashboard_token']}" if cfg["secrets"]["dashboard_token"] else ""))
        srv.serve_forever()
    elif a.cmd == "analyze":
        _analyze(cfg, a.ticker)
    elif a.cmd == "test-notify":
        from . import notify
        ok = notify.broadcast(lambda k: ("✅ <b>SEPA scanner</b> is connected." if k == "html"
                                         else "✅ *SEPA scanner* is connected.")
                              + "\n" + notify.dashboard_link(cfg), cfg)
        print(("Sent - check your phone." if ok else "Not delivered - see the warnings above.")
              + " Channels:", cfg["notifications"]["channels"])
    elif a.cmd == "ibkr-check":
        _ibkr_check(cfg)
    elif a.cmd == "telegram-chat-id":
        from .notify import telegram_find_chat_id
        tok = cfg["secrets"]["telegram_bot_token"]
        if not tok:
            sys.exit("Set TELEGRAM_BOT_TOKEN in .env first, then send any message to your bot.")
        chats = telegram_find_chat_id(tok)
        print("\n".join(f"TELEGRAM_CHAT_ID={cid}   ({name})" for cid, name in chats)
              or "No messages yet - open your bot in Telegram, press Start / send 'hi', then rerun.")


if __name__ == "__main__":
    main()
