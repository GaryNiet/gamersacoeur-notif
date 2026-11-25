"""Keep the stream's progress bar up to date.

Writes the same JSON as the previous years' script and uploads it over FTP,
where the progress bar overlay page reads it:

    {"objective": 4000, "sl_charity": 1234.5, "heros_locaux": 678.0}

* heros_locaux = every TWINT donation received by this app (+ offset)
* sl_charity   = "amount_raised" of the Streamlabs Charity team

It is refreshed right after each new donation and every
``interval_seconds`` (for the Streamlabs Charity total), and only uploaded
when something changed.
"""

from __future__ import annotations

import ftplib
import io
import json
import logging
import threading
from decimal import Decimal

import requests

from .config import ProgressConfig
from .db import Database, now

log = logging.getLogger(__name__)
STATE_KEY = "progress_bar"


def _number(value: Decimal | float) -> float | int:
    v = round(float(value), 2)
    return int(v) if v == int(v) else v


class ProgressBar:
    def __init__(self, cfg: ProgressConfig, db: Database, http=None, ftp_factory=None):
        self.cfg = cfg
        self.db = db
        self.http = http or requests.Session()
        self.ftp_factory = ftp_factory or self._connect_ftp
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        state = self.status()
        self._charity = float(state.get("charity_last", 0))   # last known value
        self._last_uploaded = state.get("data")

    # --- values --------------------------------------------------------
    def fetch_charity(self) -> float:
        """Streamlabs Charity total in CHF; keeps the last known value on errors."""
        if not self.cfg.charity_url:
            return 0
        try:
            r = self.http.get(self.cfg.charity_url, timeout=15)
            r.raise_for_status()
            self._charity = int(r.json()["amount_raised"]) / 100
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            log.warning("Streamlabs Charity total unavailable (%s), keeping %s", exc,
                        self._charity)
        return self._charity

    def compute(self) -> dict:
        local = self.db.donations_total() + Decimal(str(self.cfg.offset))
        return {
            "objective": _number(self.cfg.objective),
            "sl_charity": _number(self.fetch_charity()),
            "heros_locaux": _number(local),
        }

    # --- upload --------------------------------------------------------
    def _connect_ftp(self) -> ftplib.FTP:
        f = self.cfg.ftp
        ftp = ftplib.FTP_TLS(timeout=30) if f.tls else ftplib.FTP(timeout=30)
        ftp.connect(f.host, f.port)
        ftp.login(f.username, f.password)
        if f.tls:
            ftp.prot_p()                 # encrypt the file transfer too
        return ftp

    def upload(self, data: dict) -> None:
        payload = json.dumps(data).encode("utf-8")
        ftp = self.ftp_factory()
        try:
            if self.cfg.ftp.directory:
                ftp.cwd(self.cfg.ftp.directory)
            ftp.storbinary(f"STOR {self.cfg.filename}", io.BytesIO(payload))
        finally:
            try:
                ftp.quit()
            except Exception:  # noqa: BLE001 - already uploaded or already failed
                ftp.close()

    def update(self, force: bool = False) -> dict:
        """Recompute and upload if changed (or ``force``). Returns the data."""
        with self._lock:
            data = self.compute()
            state = {"data": self._last_uploaded, "charity_last": self._charity,
                     "uploaded_at": self.status().get("uploaded_at"), "error": ""}
            if force or data != self._last_uploaded:
                try:
                    self.upload(data)
                    self._last_uploaded = data
                    state.update(data=data, uploaded_at=now())
                    log.info("Progress bar uploaded: %s", data)
                except (ftplib.all_errors) as exc:
                    state["error"] = f"{now()} FTP: {exc}"
                    log.error("Progress bar upload failed: %s", exc)
            self.db.set_state(STATE_KEY, json.dumps(state))
            return data

    def status(self) -> dict:
        return json.loads(self.db.get_state(STATE_KEY) or "{}")

    # --- background loop -----------------------------------------------
    def trigger(self) -> None:
        """Ask for an update as soon as possible (after a new donation)."""
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def run_forever(self) -> None:
        force = True                       # always publish once at start-up
        while not self._stop.is_set():
            try:
                self.update(force=force)
                force = False
            except Exception:  # noqa: BLE001 - never let the loop die
                log.exception("Progress bar update failed")
            self._wake.wait(self.cfg.interval_seconds)
            self._wake.clear()

    def start_thread(self) -> threading.Thread:
        t = threading.Thread(target=self.run_forever, name="progress-bar", daemon=True)
        t.start()
        return t
