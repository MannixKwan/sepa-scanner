"""Buy-order tracking: lifecycle, trigger detection, positions, and the web API."""
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

from minervini import orders


def bars(rows, start="2026-10-12"):
    """rows: [(open, high, low, close)] for consecutive business days."""
    idx = pd.bdate_range(start, periods=len(rows))
    return pd.DataFrame([{"open": o, "high": h, "low": l, "close": c, "volume": 1e6} for o, h, l, c in rows], index=idx)


class OrdersTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        (d / "site" / "data" / "t").mkdir(parents=True)
        self.patches = [mock.patch.object(orders, "PATH", d / "orders.json"),
                        mock.patch.object(orders, "LOCK", d / "orders.lock"),
                        mock.patch.object(orders, "SITE_DIR", d / "site"),
                        mock.patch.object(orders, "_now", return_value="2026-10-12T08:00:00-04:00")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def order(self, **kw):
        base = {"ticker": "NSIT", "type": "stop_limit", "trigger": 164.74, "limit": 172.97, "shares": 75, "stop_loss": 157.23}
        base.update(kw)
        return orders.create(base)


class LifecycleTest(OrdersTestCase):
    def test_stop_limit_triggers_and_becomes_position(self):
        o = self.order()
        orders.process({"NSIT": bars([(163, 164.2, 162, 163.9), (164, 166.5, 163.5, 166)])}, session_closed=False)
        got = orders.load()[0]
        self.assertEqual(got["status"], "TRIGGERED")
        self.assertEqual(got["fill_price_est"], 164.74)
        self.assertEqual(got["triggered_at"], "2026-10-13")
        pos = orders.as_positions(orders.load())
        self.assertEqual((pos[0]["ticker"], pos[0]["shares"], pos[0]["stop"]), ("NSIT", 75, 157.23))
        self.assertTrue(pos[0]["fill_estimated"])
        self.assertEqual([m["key"] for m in orders.outbox()], [f"order:{o['id']}:triggered"])

    def test_gap_up_fills_at_open(self):
        self.order()
        orders.process({"NSIT": bars([(168, 170, 167, 169)])}, session_closed=False)
        self.assertEqual(orders.load()[0]["fill_price_est"], 168)

    def test_gap_above_limit_does_not_fill(self):
        o = self.order()
        orders.process({"NSIT": bars([(175, 178, 174, 177)])}, session_closed=False)
        got = orders.load()[0]
        self.assertEqual(got["status"], "PENDING")
        self.assertTrue(any(":above_limit:" in m["key"] for m in orders.outbox()), orders.outbox())
        orders.ack({m["key"] for m in orders.outbox()})
        self.assertEqual(orders.outbox(), [])
        self.assertEqual(o["id"], got["id"])

    def test_limit_buy(self):
        self.order(type="limit", trigger=None, limit=160.0)
        orders.process({"NSIT": bars([(163, 164, 161, 162), (161, 162, 159.5, 160.5)])}, session_closed=False)
        self.assertEqual(orders.load()[0]["fill_price_est"], 160.0)

    def test_day_order_expires_after_the_session(self):
        self.order(expiry="DAY")
        from datetime import date
        orders.process({"NSIT": bars([(163, 164, 162, 163.5)])}, session_closed=False, today=date(2026, 10, 12))
        self.assertEqual(orders.load()[0]["status"], "PENDING")
        orders.process({"NSIT": bars([(163, 164, 162, 163.5)])}, session_closed=True, today=date(2026, 10, 12))
        self.assertEqual(orders.load()[0]["status"], "EXPIRED")

    def test_high_before_placing_does_not_trigger(self):
        # the dashboard's last bar (today, so far) already had a high above the trigger
        (orders.SITE_DIR / "data" / "t" / "NSIT.json").write_text(json.dumps({"chart": {"d": ["2026-10-12"], "h": [165.0]}}))
        self.order()
        orders.process({"NSIT": bars([(163, 165.0, 162, 163)])}, session_closed=False)
        self.assertEqual(orders.load()[0]["status"], "PENDING")
        orders.process({"NSIT": bars([(163, 165.6, 162, 165.2)])}, session_closed=False)   # new high later today
        self.assertEqual(orders.load()[0]["status"], "TRIGGERED")

    def test_user_actions(self):
        o = self.order()
        self.assertEqual(orders.as_pending_buys(orders.load())[0]["value_usd"], round(75 * 172.97, 2))
        orders.process({"NSIT": bars([(164, 166, 163, 165)])}, session_closed=False)
        orders.update(o["id"], "confirm", {"fill_price": "164.90", "shares": "70"})
        got = orders.load()[0]
        self.assertEqual((got["status"], got["fill_price"], got["shares"]), ("FILLED", 164.9, 70))
        self.assertFalse(orders.as_positions(orders.load())[0]["fill_estimated"])
        orders.update(o["id"], "close", {"exit_price": "180"})
        self.assertEqual(orders.as_positions(orders.load()), [])
        with self.assertRaises(ValueError):
            orders.update(o["id"], "cancel")              # can't cancel a closed order
        orders.update(o["id"], "delete")
        self.assertEqual(orders.load(), [])

    def test_already_bought_and_validation(self):
        orders.create({"ticker": "drh", "type": "filled", "fill_price": 12.95, "shares": 800, "stop_loss": 12.14})
        self.assertEqual(orders.as_positions(orders.load())[0]["ticker"], "DRH")
        for bad in [{"ticker": "NSIT; rm", "type": "stop"}, {"type": "stop", "trigger": -1, "shares": 5},
                    {"type": "stop_limit", "trigger": 10, "limit": 9, "shares": 5},
                    {"type": "stop", "trigger": 10, "shares": 5, "stop_loss": 11}]:
            with self.assertRaises(ValueError):
                orders.create({"ticker": "NSIT", **bad})

    def test_warnings_for_pending_orders(self):
        o = self.order()
        row = {"status": "FAILED", "days_to_earnings": 2, "plan": {"entry": 170.0, "too_wide": False}}
        w = orders.warnings(o, row)
        self.assertEqual(len(w), 3)


class PositionsMergeTest(OrdersTestCase):
    def test_orders_add_positions_without_duplicates(self):
        from minervini import pipeline
        self.order()
        orders.process({"NSIT": bars([(164, 166, 163, 165)])}, session_closed=False)
        orders.create({"ticker": "DRH", "type": "filled", "fill_price": 12.95, "shares": 800})
        pos, pending = pipeline._with_orders([{"ticker": "DRH", "shares": 800, "entry_price": 12.9, "source": "ibkr"}])
        self.assertEqual(sorted(p["ticker"] for p in pos), ["DRH", "NSIT"])   # DRH from IBKR wins
        self.assertEqual(pending, [])


class ApiTest(OrdersTestCase):
    def setUp(self):
        super().setUp()
        from minervini import server
        self.refresh = mock.patch.object(server, "_refresh_soon")
        self.refresh.start()
        cfg = {"secrets": {"dashboard_token": "tok123"}, "dashboard": {"host": "127.0.0.1", "port": 0}}
        with mock.patch.object(server, "SITE_DIR", Path(self.tmp.name) / "site"):
            self.srv = server.make_server(cfg)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.refresh.stop()
        super().tearDown()

    def call(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port)
        h = {"Cookie": "sepa_k=tok123", "Content-Type": "application/json", "X-SEPA": "1", **(headers or {})}
        c.request(method, path, body=json.dumps(body) if body is not None else None, headers={k: v for k, v in h.items() if v})
        r = c.getresponse()
        return r.status, json.loads(r.read() or b"{}")

    def test_create_list_cancel(self):
        st, j = self.call("POST", "/api/orders", {"ticker": "NSIT", "type": "stop", "trigger": 164.74, "shares": 75})
        self.assertEqual(st, 200)
        oid = j["order"]["id"]
        st, j = self.call("GET", "/api/orders")
        self.assertEqual([o["id"] for o in j["orders"]], [oid])
        st, j = self.call("POST", f"/api/orders/{oid}/cancel", {})
        self.assertEqual((st, j["order"]["status"]), (200, "CANCELLED"))

    def test_protections(self):
        order = {"ticker": "NSIT", "type": "stop", "trigger": 1, "shares": 1}
        self.assertEqual(self.call("POST", "/api/orders", order, {"Cookie": ""})[0], 401)      # not signed in
        self.assertEqual(self.call("POST", "/api/orders", order, {"X-SEPA": ""})[0], 403)      # cross-site style
        self.assertEqual(self.call("POST", "/api/orders", {"ticker": "NSIT", "type": "stop", "shares": 1})[0], 400)
        self.assertEqual(self.call("POST", "/api/orders/0123456789/cancel", {})[0], 404)


if __name__ == "__main__":
    unittest.main()
