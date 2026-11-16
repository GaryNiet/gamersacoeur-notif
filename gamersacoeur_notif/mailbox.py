"""Read new emails from an IMAP mailbox.

Emails are fetched with BODY.PEEK so they are NOT marked as read.
The last processed UID is stored in the database, so a restart picks up
exactly where it left off (and nothing is notified twice).
"""

from __future__ import annotations

import email
import imaplib
import logging
from email import policy
from email.message import EmailMessage

from .config import MailConfig
from .db import Database

log = logging.getLogger(__name__)


class Mailbox:
    def __init__(self, cfg: MailConfig, db: Database):
        self.cfg = cfg
        self.db = db
        self.conn: imaplib.IMAP4 | None = None
        self._key = f"{cfg.username}@{cfg.host}/{cfg.folder}"

    def connect(self) -> None:
        cls = imaplib.IMAP4_SSL if self.cfg.use_ssl else imaplib.IMAP4
        self.conn = cls(self.cfg.host, self.cfg.port, timeout=60)
        self.conn.login(self.cfg.username, self.cfg.password)
        typ, _ = self.conn.select(_quote(self.cfg.folder), readonly=True)
        if typ != "OK":
            raise RuntimeError(f"Cannot open folder {self.cfg.folder}")
        self._check_uidvalidity()

    def close(self) -> None:
        if self.conn is not None:
            try:
                self.conn.logout()
            except Exception:  # noqa: BLE001 - best effort
                pass
            self.conn = None

    # ------------------------------------------------------------------
    def _all_uids(self, start: int = 1) -> list[int]:
        typ, data = self.conn.uid("SEARCH", None, f"UID {start}:*")
        if typ != "OK":
            raise RuntimeError(f"IMAP search failed: {data}")
        return [int(x) for x in (data[0] or b"").split()]

    def _check_uidvalidity(self) -> None:
        typ, data = self.conn.response("UIDVALIDITY")
        validity = data[0].decode() if data and data[0] else ""
        stored = self.db.get_state(f"uidvalidity:{self._key}")
        last = self.db.get_state(f"last_uid:{self._key}")
        if last is None or stored != validity:
            if last is not None:
                log.warning("UIDVALIDITY changed; resetting mailbox position")
            if self.cfg.process_existing and last is None:
                baseline = 0
            else:
                uids = self._all_uids()
                baseline = max(uids) if uids else 0
            self.db.set_state(f"last_uid:{self._key}", str(baseline))
            self.db.set_state(f"uidvalidity:{self._key}", validity)
            log.info("Mailbox baseline set at UID %s", baseline)

    @property
    def last_uid(self) -> int:
        return int(self.db.get_state(f"last_uid:{self._key}", "0"))

    def mark_done(self, uid: int) -> None:
        if uid > self.last_uid:
            self.db.set_state(f"last_uid:{self._key}", str(uid))

    def fetch_new(self) -> list[tuple[int, EmailMessage]]:
        """Return (uid, message) for every email newer than the last processed one."""
        self.conn.noop()  # lets the server report new mail
        last = self.last_uid
        uids = [u for u in self._all_uids(last + 1) if u > last]
        out = []
        for uid in sorted(uids):
            typ, data = self.conn.uid("FETCH", str(uid), "(BODY.PEEK[])")
            if typ != "OK" or not data or not isinstance(data[0], tuple):
                log.warning("Could not fetch UID %s", uid)
                continue
            msg = email.message_from_bytes(data[0][1], policy=policy.default)
            out.append((uid, msg))
        return out


def matches_filters(msg: EmailMessage, cfg: MailConfig) -> bool:
    sender = str(msg.get("From", "")).lower()
    subject = str(msg.get("Subject", "")).lower()
    if cfg.from_contains and not any(f.lower() in sender for f in cfg.from_contains):
        return False
    if cfg.subject_contains and not any(f.lower() in subject for f in cfg.subject_contains):
        return False
    return True


def _quote(folder: str) -> str:
    return folder if folder.isalnum() else '"' + folder.replace('"', '\\"') + '"'
