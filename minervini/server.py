"""Dashboard web server (static files) with an optional access token.

Open  http://host:port/?k=<DASHBOARD_TOKEN>  once; a cookie keeps you signed in.
For phone access use Tailscale (private) or a Cloudflare Tunnel (public URL + token).
"""
from __future__ import annotations

import hmac
import logging
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .config import SITE_DIR

log = logging.getLogger(__name__)
COOKIE = "sepa_k"


class Handler(SimpleHTTPRequestHandler):
    token: str = ""

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

    def do_GET(self):
        ok, from_link = self._authorised()
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
    SITE_DIR.mkdir(exist_ok=True)
    handler = partial(Handler, directory=str(SITE_DIR))
    return ThreadingHTTPServer((cfg["dashboard"]["host"], int(cfg["dashboard"]["port"])), handler)


def serve_background(cfg: dict) -> ThreadingHTTPServer:
    srv = make_server(cfg)
    threading.Thread(target=srv.serve_forever, daemon=True, name="dashboard").start()
    log.info("Dashboard on http://%s:%s/", cfg["dashboard"]["host"], cfg["dashboard"]["port"])
    return srv
