"""SQLite storage: streamers (+ Streamlabs tokens), mailbox state, donations, deliveries."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal

from .config import Streamer

SCHEMA = """
-- One row per streamer, created when they log in with Streamlabs.
-- keywords: JSON list of words that route a donation email to them.
CREATE TABLE IF NOT EXISTS streamers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    streamlabs_id TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    keywords TEXT NOT NULL DEFAULT '[]',
    is_main INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    access_token TEXT NOT NULL,
    refresh_token TEXT,
    connected_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS donations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT UNIQUE,
    received_at TEXT NOT NULL,
    subject TEXT,
    donor TEXT,
    amount TEXT,
    currency TEXT,
    message TEXT,
    routed_to TEXT,
    status TEXT NOT NULL,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    donation_id INTEGER,
    streamer_id TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT,
    sent_at TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        with self._lock:
            conn = sqlite3.connect(self.path, timeout=30)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                conn.commit()
            finally:
                conn.close()

    # --- state -----------------------------------------------------------
    def get_state(self, key: str, default: str | None = None) -> str | None:
        with self._conn() as c:
            row = c.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_state(self, key: str, value: str) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO state(key, value) VALUES(?, ?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    # --- streamers ------------------------------------------------------
    def upsert_streamer(self, streamlabs_id: str, name: str | None, keywords: list[str],
                        access_token: str, refresh_token: str | None, *, main: bool = False,
                        enabled: bool = True) -> tuple[int, bool]:
        """Create the streamer on first login, or refresh their tokens.

        On a re-login, keywords / main / enabled set by the admin are kept.
        Returns (id, created).
        """
        with self._conn() as c:
            row = c.execute("SELECT id FROM streamers WHERE streamlabs_id=?",
                            (streamlabs_id,)).fetchone()
            if row:
                c.execute("UPDATE streamers SET name=COALESCE(?, name), access_token=?, "
                          "refresh_token=COALESCE(?, refresh_token), connected_at=?, "
                          "is_main=MAX(is_main, ?) WHERE id=?",
                          (name, access_token, refresh_token, now(), int(main), row["id"]))
                return row["id"], False
            cur = c.execute(
                "INSERT INTO streamers(streamlabs_id, name, keywords, is_main, enabled, "
                "access_token, refresh_token, connected_at) VALUES(?,?,?,?,?,?,?,?)",
                (streamlabs_id, name or "(nom inconnu)", json.dumps(keywords), int(main),
                 int(enabled), access_token, refresh_token, now()))
            return cur.lastrowid, True

    @staticmethod
    def _to_streamer(row: sqlite3.Row) -> Streamer:
        return Streamer(id=str(row["id"]), name=row["name"],
                        keywords=tuple(json.loads(row["keywords"])), main=bool(row["is_main"]))

    def streamers(self) -> list[Streamer]:
        """Enabled streamers, in sign-up order."""
        with self._conn() as c:
            rows = c.execute("SELECT * FROM streamers WHERE enabled=1 ORDER BY id").fetchall()
        return [self._to_streamer(r) for r in rows]

    def main_streamers(self) -> list[Streamer]:
        return [s for s in self.streamers() if s.main]

    def get_streamer(self, streamer_id) -> Streamer | None:
        row = self.get_streamer_row(streamer_id)
        return self._to_streamer(row) if row and row["enabled"] else None

    def get_streamer_row(self, streamer_id) -> sqlite3.Row | None:
        with self._conn() as c:
            return c.execute("SELECT * FROM streamers WHERE id=?", (streamer_id,)).fetchone()

    def streamer_rows(self) -> list[sqlite3.Row]:
        with self._conn() as c:
            return c.execute("SELECT * FROM streamers ORDER BY is_main DESC, id").fetchall()

    def get_token(self, streamer_id) -> sqlite3.Row | None:
        """Row with access_token / refresh_token, or None if unknown."""
        return self.get_streamer_row(streamer_id)

    def update_tokens(self, streamer_id, access_token: str, refresh_token: str | None) -> None:
        with self._conn() as c:
            c.execute("UPDATE streamers SET access_token=?, "
                      "refresh_token=COALESCE(?, refresh_token) WHERE id=?",
                      (access_token, refresh_token, streamer_id))

    def update_streamer(self, streamer_id, *, keywords: list[str], main: bool,
                        enabled: bool) -> None:
        with self._conn() as c:
            c.execute("UPDATE streamers SET keywords=?, is_main=?, enabled=? WHERE id=?",
                      (json.dumps(keywords), int(main), int(enabled), streamer_id))

    def delete_streamer(self, streamer_id) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM streamers WHERE id=?", (streamer_id,))

    def add_test_streamer(self, name: str, keywords=(), main=False, token=None) -> Streamer:
        """Helper for tests and manual setup."""
        sid, _ = self.upsert_streamer(f"manual:{name}", name, list(keywords),
                                      token or f"tok-{name}", None, main=main)
        return self.get_streamer(sid)

    # --- donations -------------------------------------------------------
    def donation_exists(self, message_id: str) -> bool:
        with self._conn() as c:
            return c.execute("SELECT 1 FROM donations WHERE message_id=?",
                             (message_id,)).fetchone() is not None

    def add_donation(self, *, message_id, subject, donor, amount, currency, message,
                     routed_to, status, detail=None) -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO donations(message_id, received_at, subject, donor, amount, currency, "
                "message, routed_to, status, detail) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (message_id, now(), subject, donor, amount, currency, message,
                 ",".join(routed_to), status, detail),
            )
            return cur.lastrowid

    def set_donation_status(self, donation_id: int, status: str, detail: str | None = None):
        """Update the status; the detail is only replaced when one is given."""
        with self._conn() as c:
            c.execute("UPDATE donations SET status=?, detail=COALESCE(?, detail) WHERE id=?",
                      (status, detail, donation_id))

    def get_donation(self, donation_id: int) -> sqlite3.Row | None:
        with self._conn() as c:
            return c.execute("SELECT * FROM donations WHERE id=?", (donation_id,)).fetchone()

    def donations_total(self) -> Decimal:
        """Sum of every donation whose amount could be read."""
        with self._conn() as c:
            rows = c.execute("SELECT amount FROM donations WHERE amount IS NOT NULL").fetchall()
        return sum((Decimal(r["amount"]) for r in rows), Decimal("0"))

    def recent_donations(self, limit: int = 50) -> list[sqlite3.Row]:
        with self._conn() as c:
            return c.execute("SELECT * FROM donations ORDER BY id DESC LIMIT ?",
                             (limit,)).fetchall()

    # --- deliveries ------------------------------------------------------
    def add_delivery(self, donation_id, streamer_id, status, detail=None) -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO deliveries(donation_id, streamer_id, status, detail, sent_at) "
                "VALUES(?,?,?,?,?)", (donation_id, str(streamer_id), status, detail, now()))
            return cur.lastrowid

    def update_delivery(self, delivery_id: int, status: str, detail: str | None = None) -> None:
        with self._conn() as c:
            c.execute("UPDATE deliveries SET status=?, detail=?, sent_at=? WHERE id=?",
                      (status, detail, now(), delivery_id))

    def donations_with_queued(self) -> list[int]:
        with self._conn() as c:
            rows = c.execute("SELECT DISTINCT donation_id FROM deliveries WHERE status='queued' "
                             "ORDER BY donation_id").fetchall()
        return [r["donation_id"] for r in rows]

    def mark_queued_interrupted(self, donation_id: int) -> None:
        with self._conn() as c:
            c.execute("UPDATE deliveries SET status='interrupted', detail='app restarted' "
                      "WHERE donation_id=? AND status='queued'", (donation_id,))

    def deliveries_for(self, donation_id: int) -> list[sqlite3.Row]:
        with self._conn() as c:
            return c.execute("SELECT * FROM deliveries WHERE donation_id=? ORDER BY id",
                             (donation_id,)).fetchall()

    def sent_or_queued_ids(self, donation_id: int) -> set[str]:
        with self._conn() as c:
            rows = c.execute("SELECT streamer_id FROM deliveries WHERE donation_id=? "
                             "AND status IN ('sent', 'dry_run', 'queued')", (donation_id,)).fetchall()
        return {r["streamer_id"] for r in rows}
