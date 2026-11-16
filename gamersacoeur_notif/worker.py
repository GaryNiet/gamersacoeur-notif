"""Background loop: poll the mailbox and notify Streamlabs."""

from __future__ import annotations

import logging
import threading
from email.message import EmailMessage
from email.utils import make_msgid

from .config import Config
from .db import Database, now
from .mailbox import Mailbox, matches_filters
from .notifier import Notifier, overall_status
from .parsing import email_text, find_streamer, parse_donation

log = logging.getLogger(__name__)


def process_message(cfg: Config, db: Database, notifier: Notifier,
                    msg: EmailMessage) -> int | None:
    """Handle one email. Returns the donation id, or None if it was ignored."""
    if not matches_filters(msg, cfg.mail):
        return None
    message_id = str(msg.get("Message-ID") or "").strip() or make_msgid()
    if db.donation_exists(message_id):
        log.info("Already processed %s", message_id)
        return None

    subject = str(msg.get("Subject", ""))
    parsed = parse_donation(subject, email_text(msg), cfg.parsing)
    routed = find_streamer(parsed.streamer_hint, cfg.streamers)
    routed_to = [routed.slug] if routed else []

    if parsed.amount is None:
        donation_id = db.add_donation(
            message_id=message_id, subject=subject, donor=parsed.donor, amount=None,
            currency=parsed.currency, message=parsed.message, routed_to=routed_to,
            status="unparsed", detail="amount not found - check parsing.amount regex")
        log.warning("Could not find an amount in email %r", subject)
        return donation_id

    donation_id = db.add_donation(
        message_id=message_id, subject=subject, donor=parsed.donor, amount=str(parsed.amount),
        currency=parsed.currency, message=parsed.message, routed_to=routed_to,
        status="pending", detail=None if routed else "no streamer matched - main only")
    log.info("Donation #%s: %s %s from %s -> %s", donation_id, parsed.amount,
             parsed.currency, parsed.donor, routed.slug if routed else "(main only)")
    notifier.deliver(donation_id, notifier.targets(routed), parsed.donor, parsed.amount,
                     parsed.currency, parsed.message)
    db.set_donation_status(donation_id, overall_status(db, donation_id),
                           None if routed else "no streamer matched - main only")
    return donation_id


class Worker:
    def __init__(self, cfg: Config, db: Database):
        self.cfg = cfg
        self.db = db
        self.notifier = Notifier(cfg, db)
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run_forever(self) -> None:
        backoff = 5
        while not self._stop.is_set():
            box = Mailbox(self.cfg.mail, self.db)
            try:
                box.connect()
                log.info("Connected to %s/%s", self.cfg.mail.host, self.cfg.mail.folder)
                self.db.set_state("worker_error", "")
                backoff = 5
                while not self._stop.is_set():
                    for uid, msg in box.fetch_new():
                        try:
                            process_message(self.cfg, self.db, self.notifier, msg)
                        except Exception:  # noqa: BLE001 - never block the mailbox
                            log.exception("Failed to process email UID %s", uid)
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
