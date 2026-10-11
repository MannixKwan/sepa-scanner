"""Telegram / WhatsApp notifications: short digests and event alerts.

Messages are kept deliberately small: a handful of actionable stocks plus your open
positions with their exit levels, and a link to the dashboard for everything else.
"""
from __future__ import annotations

import html
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

STATUS_ICON = {"BREAKOUT": "🚀", "BUY ZONE": "✅", "READY": "🎯", "FORMING": "⏳",
               "EXTENDED": "⚠️", "FAILED": "❌", "TREND": "📈"}
ACTION_ICON = {"EXIT": "🔴", "SELL PARTIAL": "💰", "TIGHTEN": "🟠", "HOLD": "🟢"}
REGIME_ICON = {"uptrend": "🟢", "pressure": "🟡", "correction": "🔴"}


class Fmt:
    """Tiny markup layer so one builder produces Telegram HTML and WhatsApp text."""

    def __init__(self, kind: str):
        self.kind = kind

    def esc(self, s) -> str:
        return html.escape(str(s), quote=False) if self.kind == "html" else str(s)

    def b(self, s) -> str:
        return f"<b>{self.esc(s)}</b>" if self.kind == "html" else f"*{s}*"

    def i(self, s) -> str:
        return f"<i>{self.esc(s)}</i>" if self.kind == "html" else f"_{s}_"

    def link(self, text, url) -> str:
        return f'<a href="{html.escape(url)}">{self.esc(text)}</a>' if self.kind == "html" else f"{text}: {url}"


def _p(x: float | None, nd: int = 0, sign: bool = True) -> str:
    if x is None:
        return "–"
    if abs(x) >= 10:                                  # growth off a near-zero base
        return ">+999%" if x > 0 else "<−999%"
    return f"{x * 100:+.{nd}f}%" if sign else f"{x * 100:.{nd}f}%"


def _k(x: float | None) -> str:
    if x is None:
        return "–"
    return f"${x / 1e6:.2f}M" if abs(x) >= 1e6 else f"${x / 1e3:.1f}k" if abs(x) >= 1e4 else f"${x:,.0f}"


def _when(cfg: dict) -> str:
    now = datetime.now(ET)
    local = now.astimezone(ZoneInfo(cfg["notifications"]["local_timezone"]))
    return f"{now:%a %d %b, %H:%M} ET / {local:%H:%M} {local.tzname()}"


def dashboard_link(cfg: dict, path: str = "") -> str:
    url = cfg["secrets"].get("dashboard_url") or f"http://localhost:{cfg['dashboard']['port']}/"
    tok = cfg["secrets"].get("dashboard_token")
    if tok and "k=" not in url:
        url += ("&" if "?" in url else "?") + f"k={tok}"
    return url + (f"#/t/{path}" if path else "")


def _pick_lines(f: Fmt, r: dict, k: int) -> list[str]:
    p = r["plan"] or {}
    m = r.get("fund") or {}
    st = r["status"]
    where = (f"{abs(r['pivot_dist']) * 100:.1f}% below pivot" if (r.get("pivot_dist") or 0) < 0
             else f"{(r.get('pivot_dist') or 0) * 100:.1f}% above pivot")
    z = p.get("sizing") or {}
    size = (f"{p.get('shares', 0)} sh ≈ {_k(z.get('value'))} ({_p(z.get('pct_capital'), 0, sign=False)})"
            if p.get("shares") else f"0 sh - {z.get('limit', 'no budget')}")
    lines = [
        f"{k}. {f.b(r['ticker'])} {r['price']:,.2f} ({_p(r['change_pct'], 1)}) · {STATUS_ICON.get(st, '')} {f.i(st)}, {where}",
        f"    Buy > {p.get('entry', 0):,.2f} (max {p.get('buy_zone_hi', 0):,.2f}) · Stop {p.get('stop', 0):,.2f} "
        f"(−{p.get('risk_pct', 0) * 100:.1f}%)",
        f"    Size: {f.esc(size)}",
    ]
    facts = [f"RS {r['rs']}"]
    if m.get("eps_q_yoy") is not None:
        facts.append(f"EPS {_p(m['eps_q_yoy'])}")
    if m.get("sales_q_yoy") is not None:
        facts.append(f"Sales {_p(m['sales_q_yoy'])}")
    if r.get("setup"):
        facts.append(f"VCP {r['setup']['count']}T")
    if r.get("breakout"):
        b = r["breakout"]
        facts.append(f"{'🔥 ' if b['strength'] == 'power' else ''}breakout vol {b['volume_ratio']:.1f}× ({b['strength']})")
    if r.get("volume_profile"):
        facts.append(f"Vol {r['volume_profile']['grade']}")
    if (r.get("setup") or {}).get("base_number"):
        facts.append(f"base #{r['setup']['base_number']}")
    if r.get("days_to_earnings") is not None and 0 <= r["days_to_earnings"] <= 14:
        facts.append(f"⚠️ earnings {r['days_to_earnings']}d")
    lines.append("    " + " · ".join(facts))
    return lines


def _position_line(f: Fmt, p: dict) -> list[str]:
    icon = ACTION_ICON.get(p["action"], "")
    head = (f"{f.b(p['ticker'])} {p['price']:,.2f} ({_p(p['gain_pct'], 1)}, {p['r_multiple']:+.1f}R) · "
            f"stop {p['stop']:,.2f} · {icon} {f.b(p['action'])}")
    out = [head]
    top = next((s for s in p["signals"] if s["action"] == p["action"]), None)
    if top and p["action"] != "HOLD":
        out.append("    " + f.esc(top["text"]))
    else:
        t = p["targets"]
        out.append(f"    Exit if < {p['stop']:,.2f} ({p['stop_reason']}) · trim at {t['+20%']:,.2f} (+20%)")
    return out


def build_digest(summary: dict, cfg: dict, kind: str, title: str = "SEPA watchlist") -> str:
    f = Fmt(kind)
    mk = summary["market"]
    bm = mk["benchmark"]
    c = summary.get("counts", {})
    lines = [
        f"{f.b(title)} · {_when(cfg)}",
        f"{REGIME_ICON.get(mk['regime'], '')} {f.esc(mk['label'])} · {bm['symbol']} {bm['close']:,.2f} ({_p(bm['change_pct'], 1)})"
        f" · {c.get('tt_pass', 0)} pass Trend Template",
    ]
    pf = summary.get("portfolio")
    if pf:
        lines.append(
            f"💼 {f.b(pf['mode_label'] + ' mode')} · invested {_p(pf['exposure_pct'], 0, sign=False)} of {_k(pf['capital'])}"
            f" (max {_p(pf['target_exposure_pct'], 0, sign=False)}) · open risk {_p(pf['open_risk_pct'], 1, sign=False)}"
            f" · room {_k(pf['budget'])}, {pf['slots']} slot{'s' if pf['slots'] != 1 else ''}")
    lines.append("")
    picks = summary.get("top_picks", [])
    if picks:
        lines.append(f.b("🎯 Stocks to watch"))
        for k, r in enumerate(picks, 1):
            lines += _pick_lines(f, r, k)
    else:
        lines.append(f.i("No actionable VCP setups right now - patience is a position."))

    pos = summary.get("positions", [])
    if pos:
        lines += ["", f.b("💼 Your positions")]
        for p in pos:
            lines += _position_line(f, p)
    lines += ["", f.link("📊 Open dashboard", dashboard_link(cfg))]
    return "\n".join(lines)


def build_alerts(events: list[dict], cfg: dict, kind: str) -> str:
    f = Fmt(kind)
    lines = [f"{f.b('⚡ SEPA alert')} · {_when(cfg)}"]
    for e in events:
        lines.append(f"{e['icon']} {f.b(e['ticker'])} {f.esc(e['text'])}")
    lines += ["", f.link("📊 Dashboard", dashboard_link(cfg))]
    return "\n".join(lines)


# ------------------------------------------------------------------ senders
def _chunks(text: str, size: int) -> list[str]:
    out, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > size and cur:
            out.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        out.append(cur)
    return out


def send_telegram(text: str, cfg: dict) -> bool:
    s = cfg["secrets"]
    if not (s["telegram_bot_token"] and s["telegram_chat_id"]):
        log.warning("Telegram not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)")
        return False
    ok = True
    for part in _chunks(text, 3900):
        try:
            r = requests.post(f"https://api.telegram.org/bot{s['telegram_bot_token']}/sendMessage",
                              json={"chat_id": s["telegram_chat_id"], "text": part, "parse_mode": "HTML",
                                    "disable_web_page_preview": True}, timeout=20)
            if r.status_code != 200:
                log.warning("Telegram error %s: %s", r.status_code, r.text[:200])
                ok = False
        except requests.RequestException as e:
            log.warning("Telegram send failed: %s", e)
            ok = False
    return ok


def send_callmebot(text: str, cfg: dict) -> bool:
    s = cfg["secrets"]
    if not (s["callmebot_phone"] and s["callmebot_apikey"]):
        log.warning("CallMeBot not configured (CALLMEBOT_PHONE / CALLMEBOT_APIKEY)")
        return False
    ok = True
    for part in _chunks(text, 1500):
        try:
            r = requests.get("https://api.callmebot.com/whatsapp.php",
                             params={"phone": s["callmebot_phone"], "text": part, "apikey": s["callmebot_apikey"]},
                             timeout=30)
            ok &= r.status_code == 200
        except requests.RequestException as e:
            log.warning("CallMeBot send failed: %s", e)
            ok = False
    return ok


def send_twilio(text: str, cfg: dict) -> bool:
    s = cfg["secrets"]
    if not (s["twilio_sid"] and s["twilio_token"] and s["twilio_to"]):
        log.warning("Twilio not configured (TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_WHATSAPP_TO)")
        return False
    ok = True
    for part in _chunks(text, 1500):
        try:
            r = requests.post(f"https://api.twilio.com/2010-04-01/Accounts/{s['twilio_sid']}/Messages.json",
                              data={"From": s["twilio_from"], "To": s["twilio_to"], "Body": part},
                              auth=(s["twilio_sid"], s["twilio_token"]), timeout=20)
            if r.status_code >= 300:
                log.warning("Twilio error %s: %s", r.status_code, r.text[:200])
                ok = False
        except requests.RequestException as e:
            log.warning("Twilio send failed: %s", e)
            ok = False
    return ok


def broadcast(build, cfg: dict) -> bool:
    """`build(kind)` returns the message for 'html' (Telegram) or 'wa' (WhatsApp).
    True if at least one channel delivered it."""
    ok = False
    for ch in cfg["notifications"]["channels"]:
        if ch == "telegram":
            ok |= send_telegram(build("html"), cfg)
        elif ch == "whatsapp_callmebot":
            ok |= send_callmebot(build("wa"), cfg)
        elif ch == "whatsapp_twilio":
            ok |= send_twilio(build("wa"), cfg)
        else:
            log.warning("unknown notification channel %r", ch)
    return ok


def telegram_find_chat_id(token: str) -> list[tuple[str, str]]:
    r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=20).json()
    seen = {}
    for u in r.get("result", []):
        chat = (u.get("message") or u.get("channel_post") or {}).get("chat")
        if chat:
            seen[str(chat["id"])] = chat.get("username") or chat.get("title") or chat.get("first_name", "")
    return list(seen.items())
