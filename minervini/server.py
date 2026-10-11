"""Dashboard web server (static files) with an optional access token.

Open  http://host:port/?k=<DASHBOARD_TOKEN>  once; a cookie keeps you signed in.
For phone access use Tailscale (private) or a Cloudflare Tunnel (public URL + token).
"""
from __future__ import annotations

import hmac
import json
import logging
import re
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .config import SITE_DIR

log = logging.getLogger(__name__)
COOKIE = "sepa_k"


_ORDER_ACTION = re.compile(r"^/api/orders/([0-9a-f]{10})/(cancel|confirm|close|stop|delete)$")


def _refresh_soon(cfg: dict) -> None:
    """Recompute the dashboard in the background after an order change (no messages sent)."""
    def job():
        from .marketclock import Market
        from .pipeline import RUN_LOCK, intraday_refresh
        if not RUN_LOCK.acquire(blocking=False):
            return                      # a scan/refresh is running and will pick the change up
        try:
            prog = Market().progress()
            intraday_refresh(cfg, minutes_since_open=prog[0] if prog else None,
                             session_minutes=prog[1] if prog else 390, quiet=True)
        except Exception:
            log.exception("background refresh after order change failed")
        finally:
            RUN_LOCK.release()
    threading.Thread(target=job, daemon=True, name="order-refresh").start()


class Handler(SimpleHTTPRequestHandler):
    token: str = ""
    cfg: dict = {}

    def log_message(self, fmt, *args):  # keep the console quiet
        log.debug("http %s", fmt % args)

    def _authorised(self) -> tuple[bool, bool]:
        if not self.token:
            return True, False
        q = parse_qs(urlparse(self.path).query).get("k", [""])[0]
        if q and hmac.compare_digest(q, self.token):
            return True, True
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE and hmac.compare_digest(v, self.token):
                return True, False
        return False, False

    def _cookie(self) -> str:
        secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
        return f"{COOKIE}={self.token}; Path=/; Max-Age=31536000; HttpOnly; SameSite=Lax{secure}"

    def end_headers(self):
        if self.path.split("?")[0].endswith(".json") or self.path.split("?")[0] in ("/", "/index.html"):
            self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        super().end_headers()

    def _json(self, code: int, obj) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        super().end_headers()
        self.wfile.write(body)

    def do_POST(self):
        """Order API. Needs the dashboard login and a custom header (blocks cross-site requests)."""
        from . import orders
        ok, _ = self._authorised()
        if not ok:
            return self._json(401, {"error": "unauthorised"})
        if self.headers.get("X-SEPA") != "1" or "json" not in (self.headers.get("Content-Type") or ""):
            return self._json(403, {"error": "forbidden"})
        n = int(self.headers.get("Content-Length") or 0)
        if n > 10_000:
            return self._json(413, {"error": "too large"})
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("expected a JSON object")
            path = urlparse(self.path).path
            if path == "/api/orders":
                result = orders.create(body)
            elif m := _ORDER_ACTION.match(path):
                result = orders.update(m.group(1), m.group(2), body)
            else:
                return self._json(404, {"error": "not found"})
        except KeyError as e:
            return self._json(404, {"error": str(e).strip("'")})
        except ValueError as e:
            return self._json(400, {"error": str(e)})
        _refresh_soon(self.cfg)
        return self._json(200, {"order": result})

    def do_GET(self):
        ok, from_link = self._authorised()
        if ok and urlparse(self.path).path == "/api/orders":
            from . import orders
            return self._json(200, {"orders": orders.load()})
        if not ok:
            self.send_response(401)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Unauthorised - open the link from your Telegram/WhatsApp message (it carries ?k=...).")
            return
        if from_link:
            # remember the login in a cookie, then drop ?k= from the address bar (and browser history);
            # the #/t/TICKER fragment is kept by the browser across the redirect
            u = urlparse(self.path)
            rest = "&".join(kv for kv in u.query.split("&") if kv and not kv.startswith("k="))
            self.send_response(302)
            self.send_header("Set-Cookie", self._cookie())
            self.send_header("Location", u.path + (f"?{rest}" if rest else ""))
            self.end_headers()
            return
        super().do_GET()

    def do_HEAD(self):
        ok, _ = self._authorised()
        if not ok:
            self.send_response(401)
            self.end_headers()
            return
        super().do_HEAD()


def make_server(cfg: dict) -> ThreadingHTTPServer:
    Handler.token = cfg["secrets"].get("dashboard_token", "")
    Handler.cfg = cfg
    SITE_DIR.mkdir(exist_ok=True)
    handler = partial(Handler, directory=str(SITE_DIR))
    return ThreadingHTTPServer((cfg["dashboard"]["host"], int(cfg["dashboard"]["port"])), handler)


def serve_background(cfg: dict) -> ThreadingHTTPServer:
    srv = make_server(cfg)
    threading.Thread(target=srv.serve_forever, daemon=True, name="dashboard").start()
    log.info("Dashboard on http://%s:%s/", cfg["dashboard"]["host"], cfg["dashboard"]["port"])
    return srv
