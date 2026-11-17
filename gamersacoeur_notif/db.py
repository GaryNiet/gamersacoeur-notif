"""SQLite storage: Streamlabs tokens, mailbox state, donations and deliveries."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

SCHEMA = """
-- One row per Streamlabs account that logged in. slug is NULL while the
-- account is not (yet) linked to a streamer of the config.
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    streamlabs_id TEXT UNIQUE NOT NULL,
    streamlabs_name TEXT,
    slug TEXT UNIQUE,
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

    # --- Streamlabs accounts --------------------------------------------
    def save_account(self, streamlabs_id, streamlabs_name, access_token, refresh_token,
                     slug=None) -> int:
        """Insert or update an account (keyed by its Streamlabs id).

        If ``slug`` is given the account is linked to that streamer (and any
        other account linked to it is unlinked). If not, an existing link is kept.
        """
        with self._conn() as c:
            if slug:
                c.execute("UPDATE accounts SET slug=NULL WHERE slug=? AND streamlabs_id<>?",
                          (slug, streamlabs_id))
            c.execute(
                "INSERT INTO accounts(streamlabs_id, streamlabs_name, slug, access_token, "
                "refresh_token, connected_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(streamlabs_id) DO UPDATE SET "
                "streamlabs_name=COALESCE(excluded.streamlabs_name, accounts.streamlabs_name), "
                "slug=COALESCE(excluded.slug, accounts.slug), "
                "access_token=excluded.access_token, "
                "refresh_token=COALESCE(excluded.refresh_token, accounts.refresh_token), "
                "connected_at=excluded.connected_at",
                (streamlabs_id, streamlabs_name, slug, access_token, refresh_token, now()))
            return c.execute("SELECT id FROM accounts WHERE streamlabs_id=?",
                             (streamlabs_id,)).fetchone()["id"]

    def get_account(self, account_id: int) -> sqlite3.Row | None:
        with self._conn() as c:
            return c.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()

    def get_token(self, slug: str) -> sqlite3.Row | None:
        """The account linked to streamer ``slug`` (has access_token/refresh_token)."""
        with self._conn() as c:
            return c.execute("SELECT * FROM accounts WHERE slug=?", (slug,)).fetchone()

    def all_tokens(self) -> dict[str, sqlite3.Row]:
        with self._conn() as c:
            return {r["slug"]: r for r in c.execute("SELECT * FROM accounts WHERE slug IS NOT NULL")}

    def unassigned_accounts(self) -> list[sqlite3.Row]:
        with self._conn() as c:
            return c.execute("SELECT * FROM accounts WHERE slug IS NULL ORDER BY id").fetchall()

    def assign_account(self, account_id: int, slug: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE accounts SET slug=NULL WHERE slug=?", (slug,))
            c.execute("UPDATE accounts SET slug=? WHERE id=?", (slug, account_id))

    def update_tokens(self, slug: str, access_token: str, refresh_token: str | None) -> None:
        with self._conn() as c:
            c.execute("UPDATE accounts SET access_token=?, "
                      "refresh_token=COALESCE(?, refresh_token) WHERE slug=?",
                      (access_token, refresh_token, slug))

    def delete_account(self, account_id: int) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM accounts WHERE id=?", (account_id,))

    def save_token(self, slug, access_token, refresh_token=None, streamlabs_name=None) -> int:
        """Shortcut (tests/admin): link an account straight to a streamer."""
        return self.save_account(f"manual:{slug}", streamlabs_name, access_token,
                                 refresh_token, slug=slug)

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
