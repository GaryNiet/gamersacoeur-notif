"""SQLite storage: Streamlabs tokens, mailbox state, donations and deliveries."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens (
    slug TEXT PRIMARY KEY,
    access_token TEXT NOT NULL,
    refresh_token TEXT,
    streamlabs_name TEXT,
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
    slug TEXT NOT NULL,
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

    # --- tokens ----------------------------------------------------------
    def save_token(self, slug, access_token, refresh_token=None, streamlabs_name=None):
        with self._conn() as c:
            c.execute(
                "INSERT INTO tokens(slug, access_token, refresh_token, streamlabs_name, connected_at) "
                "VALUES(?, ?, ?, ?, ?) ON CONFLICT(slug) DO UPDATE SET "
                "access_token=excluded.access_token, "
                "refresh_token=COALESCE(excluded.refresh_token, tokens.refresh_token), "
                "streamlabs_name=COALESCE(excluded.streamlabs_name, tokens.streamlabs_name), "
                "connected_at=excluded.connected_at",
                (slug, access_token, refresh_token, streamlabs_name, now()),
            )

    def get_token(self, slug: str) -> sqlite3.Row | None:
        with self._conn() as c:
            return c.execute("SELECT * FROM tokens WHERE slug=?", (slug,)).fetchone()

    def all_tokens(self) -> dict[str, sqlite3.Row]:
        with self._conn() as c:
            return {r["slug"]: r for r in c.execute("SELECT * FROM tokens")}

    def delete_token(self, slug: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM tokens WHERE slug=?", (slug,))

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
        with self._conn() as c:
            c.execute("UPDATE donations SET status=?, detail=? WHERE id=?",
                      (status, detail, donation_id))

    def get_donation(self, donation_id: int) -> sqlite3.Row | None:
        with self._conn() as c:
            return c.execute("SELECT * FROM donations WHERE id=?", (donation_id,)).fetchone()

    def recent_donations(self, limit: int = 50) -> list[sqlite3.Row]:
        with self._conn() as c:
            return c.execute("SELECT * FROM donations ORDER BY id DESC LIMIT ?",
                             (limit,)).fetchall()

    # --- deliveries ------------------------------------------------------
    def add_delivery(self, donation_id, slug, status, detail=None) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO deliveries(donation_id, slug, status, detail, sent_at) "
                      "VALUES(?,?,?,?,?)", (donation_id, slug, status, detail, now()))

    def deliveries_for(self, donation_id: int) -> list[sqlite3.Row]:
        with self._conn() as c:
            return c.execute("SELECT * FROM deliveries WHERE donation_id=? ORDER BY id",
                             (donation_id,)).fetchall()

    def delivered_slugs(self, donation_id: int) -> set[str]:
        with self._conn() as c:
            rows = c.execute("SELECT slug FROM deliveries WHERE donation_id=? AND status='sent'",
                             (donation_id,)).fetchall()
        return {r["slug"] for r in rows}
