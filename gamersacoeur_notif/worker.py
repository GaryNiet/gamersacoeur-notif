"""Background loop: poll the mailbox and notify Streamlabs."""

from __future__ import annotations

import logging
import threading
from email.message import EmailMessage
from email.utils import make_msgid

from .config import Config
from .db import Database, now
from .mailbox import matches_filters, open_mailbox
from .notifier import Notifier
from .parsing import ParsedDonation, email_text, find_streamer, parse_email

log = logging.getLogger(__name__)


def process_message(cfg: Config, db: Database, notifier: Notifier,
                    msg: EmailMessage) -> int | None:
    """Handle one email. Returns the (first) new donation id, or None if ignored.

    An email holding several donations (see parsing.split_on) creates one
    donation per block, with ids "<Message-ID>#1", "#2", ...
    """
    if not matches_filters(msg, cfg.mail):
        return None
    message_id = str(msg.get("Message-ID") or "").strip() or make_msgid()
    subject = str(msg.get("Subject", ""))
    text = email_text(msg)
    donations = parse_email(subject, text, cfg.parsing)

    first = None
    for i, parsed in enumerate(donations, 1):
        key = message_id if len(donations) == 1 else f"{message_id}#{i}"
        if db.donation_exists(key):
            log.info("Already processed %s", key)
            continue
        donation_id = _record(cfg, db, notifier, key, subject, parsed, text)
        first = first or donation_id
    return first


def _record(cfg: Config, db: Database, notifier: Notifier, key: str, subject: str,
            parsed: ParsedDonation, text: str = "") -> int:
    """Store the donation (with every field and the full email text) and queue the alerts."""
    extra = {"fields": parsed.fields, "email_text": text or parsed.text}
    routed = find_streamer(parsed.streamer_hint, db.streamers())
    routed_to = [routed.id] if routed else []

    if parsed.amount is None:
        donation_id = db.add_donation(
            message_id=key, subject=subject, donor=parsed.donor, amount=None,
            currency=parsed.currency, message=parsed.message, routed_to=routed_to,
            status="unparsed", detail="amount not found - check parsing.amount regex", **extra)
        log.warning("Could not find an amount in email %r", subject)
        return donation_id

    donation_id = db.add_donation(
        message_id=key, subject=subject, donor=parsed.donor, amount=str(parsed.amount),
        currency=parsed.currency, message=parsed.message, routed_to=routed_to,
        status="pending", detail=None if routed else "no streamer matched - main only", **extra)
    log.info("Donation #%s: %s %s from %s -> %s", donation_id, parsed.amount,
             parsed.currency, parsed.donor, routed.name if routed else "(main only)")
    queued = notifier.deliver(donation_id, notifier.targets(routed), parsed.donor,
                              parsed.amount, parsed.currency, parsed.message)
    if not queued:
        db.set_donation_status(donation_id, "failed", "no streamer and no main account to notify")
    return donation_id


class Worker:
    def __init__(self, cfg: Config, db: Database, notifier: Notifier | None = None,
                 on_donation=None):
        self.cfg = cfg
        self.db = db
        self.notifier = notifier or Notifier(cfg, db)
        self.on_donation = on_donation      # called after each new donation
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run_forever(self) -> None:
        backoff = 5
        while not self._stop.is_set():
            box = open_mailbox(self.cfg.mail, self.db)
            try:
                box.connect()
                log.info("Connected to %s", self.cfg.mail.description)
                self.db.set_state("worker_error", "")
                backoff = 5
                while not self._stop.is_set():
                    for uid, msg in box.fetch_new():
                        try:
                            if process_message(self.cfg, self.db, self.notifier,
                                               msg) is not None and self.on_donation:
                                self.on_donation()
                        except Exception:  # noqa: BLE001 - never block the mailbox
                            log.exception("Failed to process email %s", uid)
                        box.mark_done(uid)
                    self.db.set_state("last_poll", now())
                    self._stop.wait(self.cfg.mail.poll_seconds)
            except Exception as exc:  # noqa: BLE001 - reconnect on any failure
                log.exception("Mailbox error, reconnecting in %ss", backoff)
                self.db.set_state("worker_error", f"{now()} {exc}")
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 300)
            finally:
                box.close()

    def start_thread(self) -> threading.Thread:
        t = threading.Thread(target=self.run_forever, name="mail-worker", daemon=True)
        t.start()
        return t
