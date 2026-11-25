import time
from decimal import Decimal
from email.message import EmailMessage
from pathlib import Path

import pytest
import yaml

from gamersacoeur_notif.config import config_from_dict
from gamersacoeur_notif.db import Database
from gamersacoeur_notif.notifier import Notifier
from gamersacoeur_notif.streamlabs import StreamlabsError

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def raw_cfg(monkeypatch, tmp_path):
    raw = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    raw["secret_key"] = "test-secret"
    raw["admin_password"] = "adminpw"
    # Tests use their own simple email format, independent of the example.
    raw["mail"] = {
        "provider": "imap", "host": "imap.test", "username": "dons@example.ch",
        "password": "x", "from_contains": ["noreply@payment-provider.example"],
        "subject_contains": ["Nouveau don", "New donation"],
    }
    raw["parsing"] = {
        "amount": r"Montant\s*:?\s*(?:CHF)?\s*([\d'.,’ ]+)",
        "donor": r"^(?:Nom|Donateur|Name)\s*:\s*(.+)$",
        "message": r"^Message\s*:\s*(.+)$",
        "streamer": r"^(?:Streamer|Pour|Référence|Reference)\s*:\s*(.+)$",
        "currency": "CHF", "default_donor": "Anonyme",
    }
    raw["streamlabs"]["client_id"] = "cid"
    raw["streamlabs"]["client_secret"] = "csecret"
    raw["database"] = str(tmp_path / "db.sqlite3")
    return raw


@pytest.fixture
def cfg(raw_cfg):
    return config_from_dict(raw_cfg)


@pytest.fixture
def db(cfg):
    return Database(cfg.database)


class FakeAPI:
    """Stands in for the streamlabs module."""

    def __init__(self):
        self.calls = []
        self.times = []          # time.monotonic() of each call
        self.fail_tokens = {}    # token -> StreamlabsError

    def _check(self, token):
        self.times.append((token, time.monotonic()))
        if token in self.fail_tokens:
            raise self.fail_tokens[token]

    def create_donation(self, token, **kw):
        self._check(token)
        self.calls.append(("donation", token, kw))
        return {"donation_id": 1}

    def create_alert(self, token, **kw):
        self._check(token)
        self.calls.append(("alert", token, kw))
        return {"success": True}

    def refresh_token(self, client_id, client_secret, redirect_uri, refresh):
        return {"access_token": f"new-{refresh}", "refresh_token": refresh}


@pytest.fixture
def people(db):
    """Main account + two streamers, as if they had connected."""
    return {
        "main": db.add_test_streamer("GamersACoeur", ["gamersacoeur"], main=True, token="tok-main"),
        "alice": db.add_test_streamer("Alice", ["alice", "alice_tv"], token="tok-alice"),
        "bob": db.add_test_streamer("Bob", ["bob", "bobstream"], token="tok-bob"),
    }


@pytest.fixture
def api():
    return FakeAPI()


@pytest.fixture
def notifier(cfg, db, api):
    cfg.streamlabs.min_interval_seconds = 0   # pacing has its own tests
    return Notifier(cfg, db, api=api)


def make_email(body, subject="Nouveau don TWINT", sender="noreply@payment-provider.example",
               html=None, msgid="<abc@test>"):
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = "dons@example.ch"
    msg["Subject"] = subject
    msg["Message-ID"] = msgid
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
    return msg


__all__ = ["make_email", "Decimal", "StreamlabsError"]
