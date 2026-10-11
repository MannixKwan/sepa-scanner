# SEPA Scanner: Minervini-style NASDAQ scanner with alerts

The scanner runs Mark Minervini's SEPA checklist over every NASDAQ stock: market health, the 8-point
Trend Template, RS rating, the Volatility Contraction Pattern (VCP), and earnings/sales acceleration.
For each setup it gives a pivot buy point, a stop, a position size and targets. While the market is
open it refreshes every 15 minutes and sends a short Telegram/WhatsApp message with:

* a handful of stocks to watch (buy point, stop, size), and
* your open positions with their current exit levels (stop, breakeven, sell-into-strength, 50-day MA).

Each message links to a dashboard that shows the evidence for every pick: price with the 50/150/200-day
MAs, volume, the contraction markers, the pivot and stop lines, the RS line, quarterly EPS and revenue
growth (acceleration in bold), margins, and the Trend Template checklist.

> This is a decision-support tool. It doesn't place orders. The IBKR connection is read-only.

---

## Architecture

```
                         ┌────────────── runner.py (scheduler, XNAS calendar) ──────────────┐
                         │  every 15 min while open          once a day, 45 min after close │
                         ▼                                    ▼                              │
  nasdaqtrader.com ─► universe.py ─────────────────────► pipeline.full_scan()                 │
  Yahoo Finance  ───► prices.py (cache: data/prices_daily.pkl, incremental)                   │
                         │                                    │                              │
                         │      technicals.py  Trend Template + IBD-style RS rating (1-99)    │
                         │      vcp.py         contractions, pivot, stop, volume dry-up       │
                         │      market.py      QQQ trend, distribution days, breadth         │
  SEC EDGAR  ─┐          │      fundamentals.py EPS/sales YoY, acceleration, margins, Code 33 │
  Yahoo EPS  ─┴──────────┘      strategy.py    status, trade plan, position size, score       │
                                positions.py   exit rules for what you hold                   │
  IBKR TWS/Gateway ─► ibkr.py (read-only: positions, avg cost, stop orders, NetLiq)           │
                         │                                                                    │
                         ▼                                                                    │
           pipeline.intraday_refresh()  ── live bars for watchlist + positions ──────────────┘
                         │
          ┌──────────────┼───────────────────────────┐
          ▼              ▼                           ▼
   site.py → site/   notify.py → Telegram /     state.py (dedupe alerts,
   (JSON + static    WhatsApp (CallMeBot or      first-seen dates)
    dashboard)       Twilio)
          │
   server.py (token-protected) ──► phone via Tailscale / Cloudflare Tunnel
```

**Why two jobs.** Scanning about 3,000 stocks needs 2 years of daily bars and quarterly fundamentals.
That's heavy, but it only changes once a day, so it runs after the close. Intraday, only the watchlist
(about 30 names) and your positions can trigger anything (a breakout through a pivot, a stop, an exit
signal), so the 15-minute job fetches only those and finishes in seconds.

| Module | Responsibility |
|---|---|
| `universe.py` | NASDAQ common stocks (ETFs, warrants, units, preferreds and SPACs removed); optional NYSE |
| `prices.py` | Batched Yahoo download, incremental cache, split detection, intraday volume-pace curve |
| `technicals.py` | MAs, ATR, **RS rating** (40% last quarter + 20% each of the prior three, percentile vs. universe), **Trend Template** |
| `vcp.py` | VCP detection by alternating extremes (each high is the max of what follows, each low the min), so contractions shrink by construction; pivot = high of the last contraction |
| `fundamentals.py` / `edgar.py` | Adjusted EPS, estimates, surprises and next report date from Yahoo; 12+ quarters of revenue and net income from SEC XBRL (Q4 = fiscal year − Q1..Q3); Yahoo fallback for foreign filers |
| `market.py` | QQQ vs 50/200-day, distribution days, % of stocks above the 50-day, new highs vs lows |
| `strategy.py` | Status (BREAKOUT / BUY ZONE / READY / FORMING / EXTENDED / FAILED), trade plan, sizing, score, "why" and "watch out" notes |
| `allocation.py` | Position sizing and progressive exposure: capital (IBKR net liquidation), cash, open risk, pilot/normal/full mode, the "if these trigger" plan |
| `positions.py` | Exit plan: initial stop → breakeven at 2R → +1R at 3R, sell ⅓–½ into strength, 50-day MA violation, climax run, earnings warning |
| `notify.py` | Digest and alert formatting; Telegram, CallMeBot and Twilio senders |
| `runner.py` | Scheduler on the official NASDAQ calendar (holidays, half days), catches up after sleep |

## The rules it applies

* **Market:** QQQ above a rising 50-day MA, 50-day above 200-day, and ≤ 4 distribution days gives
  "Confirmed uptrend". In a correction, picks are flagged and the guidance is to protect capital.
* **Trend Template (8/8):** price > 150 & 200-day MA; 150 > 200; 200-day rising ≥ 1 month;
  50 > 150 & 200; price > 50-day; ≥ 30% above the 52-week low; within 25% of the 52-week high; RS ≥ 70.
* **VCP:** 2–6 contractions, first ≤ 40%, last ≤ 10% (and not a one-day dip), base ≥ 3 weeks after an
  advance of ≥ 25%, pivot within 10% of the base high, volume drying up. The **base stage** is counted
  (resetting when a correction undercuts the prior base); 3rd-stage bases and later are flagged as late.
* **Volume behaviour (graded A–E, never a filter):** up/down volume ratio over 50 days and heavy-volume
  (≥ 1.5× average, ≥ 1% move) up days vs down days over 13 weeks. Accumulation lifts the score;
  distribution lowers it and adds a warning.
* **Fundamentals:** latest-quarter EPS ≥ +25% YoY (or loss → profit) and sales ≥ +20% or accelerating.
  Bonus for acceleration, margin expansion, beats, Code 33 and analysts raising estimates
  (cuts lower the score).
* **Entry:** buy-stop at the pivot, ideally on volume ≥ 140% of average, never > 5% above the pivot.
  Avoid buying right before earnings.
* **Stop:** just under the last contraction's low. If that's more than 8% below the pivot the setup
  isn't low-risk: it stays on the watchlist with a warning but is left out of Stocks to watch and alerts.
  **Size:** risk 1.25% of equity, max 25% of equity in one stock.
* **Exit:** see `positions.py`. Besides the stop, breakeven and 50-day rules: a position more than 3%
  below entry within 10 sessions is a failing breakout (tighten), and one going nowhere for 20 sessions
  is flagged as dead money. Every threshold is in `config.yaml`.

## Allocation (how much to buy)

`allocation.py` applies Minervini's money-management rules to your actual account. The numbers are
mechanical outputs of the parameters in `config.yaml → risk`; choosing those parameters is your call.

| Rule | Default |
|---|---|
| Size off the stop: shares = capital × risk per trade ÷ (entry − stop) | 1.25% risk per trade (normal mode) |
| Cap each new position | 25% of capital (12.5% in pilot mode) |
| Concentrated book | max 6 positions |
| Total open risk if every stop is hit at once | ≤ 10% of capital |
| Market sets the exposure ceiling | uptrend 100% · under pressure 50% · correction 0% |
| No margin unless enabled | sizes against cash, minus cash reserved by open buy orders |

**Progressive exposure** ("trade your way into the market"), decided from your open positions:

| Mode | When | Risk per trade | Max invested |
|---|---|---|---|
| Defensive | Market in correction | 0 (no new buys) | 0% |
| Pilot | No positions yet, or most of them under water | 0.625% | 50% |
| Normal | Some positions working (≥ +1R) | 1.25% | 100% |
| Full | ≥ 2 positions up ≥ 1R, one ≥ 2R, none losing | 2.0% | 100% |

Each stock's trade plan shows its share count and which limit binds (risk, position cap, cash or
open-risk cap). The Portfolio page sizes the best setups in priority order as if they all triggered.

---

## Setup (macOS)

```bash
cd ~/Documents/Project/Invest
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env              # then fill in Telegram etc. (see below)
.venv/bin/python -m unittest discover tests     # 29 logic tests
.venv/bin/python -m minervini scan              # first full scan (~10 min the first time)
.venv/bin/python -m minervini serve             # dashboard at http://localhost:8050
```

### 1. Telegram (recommended)
1. In Telegram, message **@BotFather** → `/newbot` and put the token in `.env` as `TELEGRAM_BOT_TOKEN`.
2. Send "hi" to your new bot, then run `.venv/bin/python -m minervini telegram-chat-id` and put the
   number in `.env` as `TELEGRAM_CHAT_ID`.
3. Run `.venv/bin/python -m minervini test-notify`.

### WhatsApp (optional)
* **CallMeBot** (free, personal): follow the steps in `.env.example`, then add `whatsapp_callmebot`
  to `notifications.channels` in `config.yaml`.
* **Twilio** (paid, sturdier): fill in the `TWILIO_*` variables and add `whatsapp_twilio`.
* Telegram works better here: links, formatting, no 24-hour session window, and no cost.

### 2. Dashboard on your phone
The message link must be reachable from your phone:
* **Tailscale** (recommended, private): install it on the Mac and the phone, then set
  `DASHBOARD_URL=http://<mac-name>.<tailnet>.ts.net:8050/`.
* **Cloudflare Tunnel** (public URL): `brew install cloudflared`, then
  `cloudflared tunnel --url http://localhost:8050`. Always set a long `DASHBOARD_TOKEN`; the link in
  your messages carries it, and the server rejects requests without it.

### 3. Interactive Brokers (read-only)
1. Download **IB Gateway** (lighter than TWS) from interactivebrokers.com → Trading → IB Gateway and log
   in with your IBKR username, password and 2FA.
2. In Gateway: **Configure → Settings → API → Settings**: tick **Read-Only API**, set the socket port
   (live **4001**, paper 4002) and keep 127.0.0.1 as a trusted IP. In TWS the equivalent is
   **Edit → Global Configuration → API → Settings**: enable "ActiveX and Socket Clients", port 7496.
3. Make `ibkr.port` in `config.yaml` match, then run `.venv/bin/python -m minervini ibkr-check`.
4. The scanner reads net liquidation, cash and buying power (converted to USD if your base currency is
   HKD), your US stock positions and average cost, SELL stop orders (used as each position's stop),
   pending BUY orders (their cash is reserved) and other holdings (they count toward capital but
   aren't managed). Entry dates and notes can still come from `positions.yaml`.
5. IBKR logs API sessions out daily and asks for 2FA about once a week. Set Gateway's
   **Lock and Exit → Auto restart** so it survives the daily reset. When it's down, the scanner falls
   back to `positions.yaml` and `risk.account_equity`.

### 4. Keep it running
```bash
cp deploy/com.sepa.scanner.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.sepa.scanner.plist
tail -f data/scanner.log
```
US market hours are 21:30–04:00 Hong Kong time (22:30–05:00 in the northern winter), so the Mac has to
stay awake overnight. The launch agent wraps the scanner in `caffeinate -i`, but a closed laptop lid
still sleeps the machine. A Mac mini, or a small Linux VPS (`deploy/sepa-scanner.service`), is more
reliable. IBKR sync needs TWS or Gateway running on that same machine.

## Running on AWS (access from anywhere)

`deploy/aws/` runs everything on one **Lightsail** instance in Singapore (4 GB, about US$24/month)
with Docker Compose:

```
 internet ──HTTPS──► caddy (Let's Encrypt, https://<ip>.sslip.io) ──► scanner (python -m minervini run)
                                                                         │ read-only API, internal network
                                                                         ▼
                                                              ib-gateway (IB Gateway + IBC)
```

Only ports 80 and 443 are public, and SSH is open only to your current IP. The IB Gateway API is
never exposed outside the server. The dashboard needs the token in your message link; after the
first visit a cookie keeps you signed in and the token is removed from the address bar.

```bash
aws login                                  # your AWS session (the CLI can't do this for you)
deploy/aws/sepa-aws.sh plan                # what will be created + monthly price (changes nothing)
deploy/aws/sepa-aws.sh up                  # create the server, upload code + config, start
deploy/aws/sepa-aws.sh credentials         # type your IBKR login (hidden), then approve on IBKR Mobile
deploy/aws/sepa-aws.sh ibkr-check          # confirm account, positions, stops come through
deploy/aws/sepa-aws.sh status | logs | ssh | allow-ip | destroy
```

Re-run `up` after you change code, `config.yaml`, `positions.yaml` or the Telegram settings in `.env`.
It pushes them to the server and restarts; the dashboard token is kept.

**IBKR on the server:**
* **Use a second IBKR username** (Client Portal → Settings → Users & Access Rights) with no trading
  permissions. If the server shares your everyday login, the sessions keep logging each other out,
  and a separate user limits what a compromised server could do.
* The password is stored only on the server (`/opt/sepa/secrets`, file mode 600) and passed to the
  gateway as a Docker secret. The API is read-only.
* IB Gateway restarts daily at 23:59 New York time without 2FA. IBKR still asks for a 2FA approval
  about once a week. While the gateway waits, the scanner keeps using the last IBKR snapshot and
  sends you a "🔌 IBKR connection down" alert.

**Cost control:** `deploy/aws/sepa-aws.sh destroy` deletes everything, which stops the billing.
Don't also run `minervini run` on your Mac once the server is live, or you'll get duplicate alerts.

## Commands
| Command | What it does |
|---|---|
| `python -m minervini run` | Scheduler + dashboard (what the launch agent runs) |
| `python -m minervini scan [--notify]` | Full scan now |
| `python -m minervini refresh [--digest]` | One 15-minute refresh now |
| `python -m minervini analyze NVDA` | Full analysis of one stock in the terminal |
| `python -m minervini serve` | Dashboard only |
| `python -m minervini test-notify` | Test your Telegram/WhatsApp setup |
| `python -m minervini ibkr-check` | Test the IBKR connection and print account, positions, stops |

## Recording your buy orders
This tool doesn't place orders. When you place one in IBKR, record it on the dashboard:
**📝 Record a buy order** on the stock page (prefilled from the trade plan: buy stop-limit at the pivot,
limit at Max buy, plan shares and stop) or on the Portfolio page for any ticker. Use "Already bought" for
fills you want tracked straight away.

* Every refresh checks pending orders against the price action since you placed them (a high made before
  you placed it doesn't count). A triggered order sends "✅ Buy order triggered…", becomes a position
  managed by the exit rules (fill price estimated until you **Confirm fill**), and **I sold it - close
  position** ends the tracking.
* Pending orders reserve cash and a position slot in the allocation, and drop out of the sizing table.
* Warnings (🧹) when a pending order should be cancelled or adjusted: the base failed, earnings are near,
  the pivot moved, or the stop is now too wide. Day orders expire after their session.
* Orders are stored in `data/orders.json` on the server (not in git). The API behind the forms needs the
  dashboard login and rejects cross-site requests.

## Notification policy (configurable)
* **Alerts, at once:** a breakout through a pivot (fundamentals must pass), a stock within 1.5% of its
  pivot, a failed breakout, any exit signal on your positions, a stop to raise, or a change in market
  status. Each alert goes out at most once per stock per day.
* **Breakouts are graded by volume.** Intraday, today's volume is projected from the stock's own
  5-minute pattern at the same time of day (last 10 sessions) and compared with the 50-day average:
  🔥 *power* (≥ 2× with the price up ≥ 2%), 🚀 *confirmed* (≥ 1.4×) or 👀 *light* (wait). A light
  breakout that later confirms, or a confirmed one that turns into a power breakout, sends one upgrade
  alert.
* **Digest:** at 09:45 and 12:30 New York time (21:45 and 00:30 HKT), plus a "plan for next session"
  after the nightly scan. Set `schedule.send_every_run: true` to get the full digest every 15 minutes.

## Data caveats
* Yahoo Finance is free but unofficial; prices can be ~15 min delayed and the API can change. If
  `yfinance` breaks, `pip install -U yfinance` usually fixes it.
* SEC data is official but raw. Companies change revenue tags or restate (e.g. after a divestiture),
  which can make one quarter's YoY look odd. Foreign filers (20-F) only get Yahoo's ~5 quarters.
* The RS rating is computed in the IBD style against NASDAQ stocks only, so it won't match IBD's
  number exactly.
* Pattern detection is mechanical. Look at the chart before you buy; the dashboard is built for that.

## Existing platforms worth knowing
| Platform | Good for | Gap vs. this tool |
|---|---|---|
| **MarketSurge** (ex-MarketSmith, IBD) | RS ratings, EPS/sales tables, pattern recognition, closest to Minervini/O'Neil | ~US$150/mo, no personal exit-plan alerts |
| **Deepvue** | Built for Minervini/O'Neil traders: Trend Template screens, RS, fast charts | Paid; no WhatsApp/Telegram plan for your positions |
| **ChartMill** | Ready-made "Minervini Trend Template" and VCP/setup-rating screens | Screens only; alerts by email |
| **TradingView** | Community Pine scripts for the Trend Template and VCP; webhook alerts can go to Telegram | Screener can't run custom Pine across all stocks; you'd still glue things together |
| **Finviz Elite** | Fast screener with SMA filters; real-time email alerts | No RS rating, VCP or EPS acceleration |
| **Minervini Private Access** | Mark Minervini's own trades and commentary | Subscription; his picks, not a scanner |

A practical combination: run this scanner for the daily shortlist and alerts, and use one of the
above (TradingView is free) when you want a second look at a chart before buying.
