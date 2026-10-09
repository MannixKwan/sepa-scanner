"""Small persistent state: which alerts were sent today, first-seen dates, last scans."""
from __future__ import annotations

import json
from datetime import date, timedelta

from .config import DATA_DIR


class State:
    def __init__(self):
        self.path = DATA_DIR / "state.json"
        try:
            self.data = json.loads(self.path.read_text())
        except Exception:
            self.data = {}
        self.data.setdefault("alerts", {})
        self.data.setdefault("first_seen", {})
        self.data.setdefault("digests", {})

    def alerted(self, key: str, day: str) -> bool:
        return key in self.data["alerts"].get(day, [])

    def mark_alert(self, key: str, day: str) -> None:
        self.data["alerts"].setdefault(day, []).append(key)

    def digest_sent(self, slot: str, day: str) -> bool:
        return slot in self.data["digests"].get(day, [])

    def mark_digest(self, slot: str, day: str) -> None:
        self.data["digests"].setdefault(day, []).append(slot)

    def first_seen(self, ticker: str) -> str:
        return self.data["first_seen"].setdefault(ticker, date.today().isoformat())

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def set(self, key: str, value) -> None:
        self.data[key] = value

    def save(self) -> None:
        cutoff = (date.today() - timedelta(days=10)).isoformat()
        for k in ("alerts", "digests"):
            self.data[k] = {d: v for d, v in self.data[k].items() if d >= cutoff}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1))
        tmp.replace(self.path)
