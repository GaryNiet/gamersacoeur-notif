"""Send a donation to the routed streamer and to every main account."""

from __future__ import annotations

import logging
import threading
from decimal import Decimal

from . import streamlabs
from .config import Config, Streamer
from .db import Database
from .pacer import Pacer

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, cfg: Config, db: Database, api=streamlabs):
        self.cfg = cfg
        self.db = db
        self.api = api
        # One queue per streamer: alerts to the same account are spaced out.
        self.pacer = Pacer(cfg.streamlabs.min_interval_seconds)
        self._status_lock = threading.Lock()

    def refresh_status(self, donation_id: int) -> None:
        # Serialised so a stale computation can never overwrite a newer one.
        with self._status_lock:
            self.db.set_donation_status(donation_id, overall_status(self.db, donation_id))

    def targets(self, routed: Streamer | None) -> list[Streamer]:
        """Routed streamer first, then the main account(s), without duplicates."""
        out: list[Streamer] = []
        for st in ([routed] if routed else []) + self.db.main_streamers():
            if st not in out:
                out.append(st)
        return out

    # ------------------------------------------------------------------
    def _call(self, token: str, donor: str, amount: Decimal, currency: str, message: str):
        sl = self.cfg.streamlabs
        if sl.mode == "donation":
            return self.api.create_donation(
                token, name=donor, amount=amount, currency=currency, message=message,
                identifier=f"twint-{streamlabs.sanitize_donor_name(donor).lower()}")
        fmt = {"donor": donor, "amount": f"{amount:.2f}", "currency": currency,
               "message": message}
        return self.api.create_alert(token, message=sl.alert_message.format(**fmt),
                                     user_message=sl.alert_user_message.format(**fmt))

    def send_to(self, streamer_id: str, donor: str, amount: Decimal, currency: str,
                message: str) -> tuple[str, str]:
        """Return (status, detail); status is 'sent', 'dry_run', 'not_connected' or 'error'."""
        if self.cfg.streamlabs.dry_run:
            return "dry_run", f"{donor} {amount} {currency}"
        tok = self.db.get_token(streamer_id)
        if tok is None:
            return "not_connected", "streamer has not connected Streamlabs"
        try:
            self._call(tok["access_token"], donor, amount, currency, message)
            return "sent", ""
        except streamlabs.StreamlabsError as exc:
            if exc.status == 401 and tok["refresh_token"]:
                try:
                    sl = self.cfg.streamlabs
                    new = self.api.refresh_token(sl.client_id, sl.client_secret,
                                                 sl.redirect_uri, tok["refresh_token"])
                    self.db.update_tokens(streamer_id, new["access_token"], new.get("refresh_token"))
                    self._call(new["access_token"], donor, amount, currency, message)
                    return "sent", "after token refresh"
                except streamlabs.StreamlabsError as exc2:
                    return "error", str(exc2)
            return "error", str(exc)

    def deliver(self, donation_id: int, targets: list[Streamer], donor: str,
                amount: Decimal, currency: str, message: str,
                skip: set[str] = frozenset()) -> list[str]:
        """Queue the donation for each target. Returns the names it was queued for.

        Sending happens in the background, at most one alert every
        ``min_interval_seconds`` per streamer; each delivery row goes from
        'queued' to its final status, and the donation status follows.
        """
        queued = []
        for st in targets:
            if st.id in skip:
                continue
            delivery_id = self.db.add_delivery(donation_id, st.id, "queued")
            ahead = self.pacer.pending(st.id)
            log.info("donation %s -> %s (#%s): queued (%s ahead)", donation_id, st.name, st.id,
                     ahead)

            def job(st=st, delivery_id=delivery_id):
                status, detail = self.send_to(st.id, donor, amount, currency, message)
                log.info("donation %s -> %s (#%s): %s %s", donation_id, st.name, st.id,
                         status, detail)
                self.db.update_delivery(delivery_id, status, detail)
                self.refresh_status(donation_id)
                return status, detail

            self.pacer.submit(st.id, job)
            queued.append(st.name)
        if queued:
            self.refresh_status(donation_id)
        return queued

    def retry(self, donation_id: int) -> list[str]:
        """Re-queue a donation for every target that hasn't received it (and isn't queued)."""
        row = self.db.get_donation(donation_id)
        if row is None or not row["amount"]:
            return []
        routed = self.db.get_streamer(row["routed_to"].split(",")[0]) if row["routed_to"] else None
        return self.deliver(donation_id, self.targets(routed), row["donor"],
                            Decimal(row["amount"]), row["currency"], row["message"] or "",
                            skip=self.db.sent_or_queued_ids(donation_id))

    def resume_interrupted(self) -> None:
        """After a restart: re-queue deliveries that were still waiting."""
        for donation_id in self.db.donations_with_queued():
            self.db.mark_queued_interrupted(donation_id)
            names = self.retry(donation_id)
            log.info("Resumed donation %s for %s", donation_id, ", ".join(names))

    def send_test(self, streamer_id: str, timeout: float = 5) -> tuple[str, str]:
        """Send a test alert, respecting the spacing. Waits up to ``timeout`` s."""
        fut = self.pacer.submit(str(streamer_id), lambda: self.send_to(
            str(streamer_id), "Test TWINT", Decimal("1.00"), self.cfg.parsing.currency,
            "Ceci est un test de Gamers à Coeur notif"))
        try:
            return fut.result(timeout=timeout)
        except TimeoutError:
            return "queued", "will be sent after the alerts already waiting"


def overall_status(db: Database, donation_id: int) -> str:
    statuses = {}
    for d in db.deliveries_for(donation_id):
        if statuses.get(d["streamer_id"]) != "sent":
            statuses[d["streamer_id"]] = d["status"]
    vals = set(statuses.values())
    if "queued" in vals:
        return "queued"
    if vals <= {"sent", "dry_run"}:
        return "sent"
    if vals & {"sent", "dry_run"}:
        return "partial"
    return "failed"
