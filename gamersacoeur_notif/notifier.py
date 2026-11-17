"""Send a donation to the routed streamer and to every main account."""

from __future__ import annotations

import logging
from decimal import Decimal

from . import streamlabs
from .config import Config, Streamer
from .db import Database

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, cfg: Config, db: Database, api=streamlabs):
        self.cfg = cfg
        self.db = db
        self.api = api

    def targets(self, routed: Streamer | None) -> list[Streamer]:
        """Routed streamer first, then the main account(s), without duplicates."""
        out: list[Streamer] = []
        for st in ([routed] if routed else []) + self.cfg.main_streamers:
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

    def send_to(self, slug: str, donor: str, amount: Decimal, currency: str,
                message: str) -> tuple[str, str]:
        """Return (status, detail); status is 'sent', 'dry_run', 'not_connected' or 'error'."""
        if self.cfg.streamlabs.dry_run:
            return "dry_run", f"{donor} {amount} {currency}"
        tok = self.db.get_token(slug)
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
                    self.db.update_tokens(slug, new["access_token"], new.get("refresh_token"))
                    self._call(new["access_token"], donor, amount, currency, message)
                    return "sent", "after token refresh"
                except streamlabs.StreamlabsError as exc2:
                    return "error", str(exc2)
            return "error", str(exc)

    def deliver(self, donation_id: int, targets: list[Streamer], donor: str,
                amount: Decimal, currency: str, message: str,
                skip: set[str] = frozenset()) -> dict[str, str]:
        results = {}
        for st in targets:
            if st.slug in skip:
                continue
            status, detail = self.send_to(st.slug, donor, amount, currency, message)
            log.info("donation %s -> %s: %s %s", donation_id, st.slug, status, detail)
            self.db.add_delivery(donation_id, st.slug, status, detail)
            results[st.slug] = status
        return results

    def retry(self, donation_id: int) -> dict[str, str]:
        """Resend a donation to every target that hasn't successfully received it."""
        row = self.db.get_donation(donation_id)
        if row is None or not row["amount"]:
            return {}
        routed = self.cfg.streamer(row["routed_to"].split(",")[0]) if row["routed_to"] else None
        results = self.deliver(donation_id, self.targets(routed), row["donor"],
                               Decimal(row["amount"]), row["currency"], row["message"] or "",
                               skip=self.db.delivered_slugs(donation_id))
        self.db.set_donation_status(donation_id, overall_status(self.db, donation_id))
        return results

    def send_test(self, slug: str) -> tuple[str, str]:
        return self.send_to(slug, "Test TWINT", Decimal("1.00"), self.cfg.parsing.currency,
                            "Ceci est un test de Gamers à Coeur notif")


def overall_status(db: Database, donation_id: int) -> str:
    statuses = {}
    for d in db.deliveries_for(donation_id):
        if statuses.get(d["slug"]) != "sent":
            statuses[d["slug"]] = d["status"]
    vals = set(statuses.values())
    if vals <= {"sent", "dry_run"}:
        return "sent"
    if vals & {"sent", "dry_run"}:
        return "partial"
    return "failed"
