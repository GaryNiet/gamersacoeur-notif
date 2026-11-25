"""The real iRaiser / Chaîne du Bonheur notification, through the example config."""
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from gamersacoeur_notif.config import _expand_env, config_from_dict
from gamersacoeur_notif.notifier import Notifier
from gamersacoeur_notif.worker import process_message

from .conftest import make_email

ROOT = Path(__file__).resolve().parent.parent
SENDER = "Chaîne du Bonheur <info@bonheur.ch>"
SUBJECT = "[iRaiser] notification #167 : CAC"

BLOCK = """Donateur: TestPrénom, TestNom
Pseudo: {pseudo}
A Streameur-euses: {streamer}
Don: {amount}
Date: 2026-11-25 18:06:19 Time: 11-25 2026 - 06h06
Message: {message}
Postal Address: TestAdresse N/A, 1010 TestVille CH
Email: donateur@example.ch
"""


def body(*blocks, total="50 CHF"):
    head = f"Montant collecté : {total}\nNombre de dons : {len(blocks)}\n\n\n\n\n"
    return head + "\n".join(BLOCK.format(**b) for b in blocks)


ONE = dict(pseudo="TestPseudo", streamer="TestAStreamer", amount="50", message="TestMessage")


@pytest.fixture
def real(monkeypatch, tmp_path, api):
    for k in ("GAC_SECRET_KEY", "GAC_ADMIN_PASSWORD", "STREAMLABS_CLIENT_ID",
              "STREAMLABS_CLIENT_SECRET", "MS_CLIENT_SECRET", "FTP_PASSWORD"):
        monkeypatch.setenv(k, "x")
    raw = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    raw["database"] = str(tmp_path / "db.sqlite3")
    cfg = config_from_dict(_expand_env(raw))
    cfg.streamlabs.min_interval_seconds = 0
    from gamersacoeur_notif.db import Database
    db = Database(cfg.database)
    db.add_test_streamer("GamersACoeur", ["GamersACoeur"], main=True, token="tok-main")
    db.add_test_streamer("TestAStreamer", ["TestAStreamer"], token="tok-streamer")
    db.add_test_streamer("Alice", ["Alice"], token="tok-alice")
    return cfg, db, Notifier(cfg, db, api=api)


def run(real, msg):
    cfg, db, notifier = real
    did = process_message(cfg, db, notifier, msg)
    notifier.pacer.wait_idle()
    return did


def test_sample_email(real, api):
    cfg, db, _ = real
    did = run(real, make_email(body(ONE), subject=SUBJECT, sender=SENDER))
    row = db.get_donation(did)
    assert Decimal(row["amount"]) == Decimal("50.00")      # "Don", not "Montant collecté"
    assert row["donor"] == "TestPseudo"                    # never the real name
    assert row["message"] == "TestMessage"
    assert [c[1] for c in api.calls] == ["tok-streamer", "tok-main"]
    assert api.calls[0][2]["name"] == "TestPseudo" and api.calls[0][2]["currency"] == "CHF"


def test_total_is_not_the_amount(real, api):
    cfg, db, _ = real
    did = run(real, make_email(body(dict(ONE, amount="20"), total="1'250 CHF"),
                               subject=SUBJECT, sender=SENDER))
    assert Decimal(db.get_donation(did)["amount"]) == Decimal("20.00")


def test_html_version(real, api):
    cfg, db, _ = real
    html = "<html><body>" + "".join(f"<p>{line}</p>" if line else "<br>"
                                    for line in body(ONE).splitlines()) + "</body></html>"
    from email.message import EmailMessage
    only_html = EmailMessage()
    only_html["From"], only_html["Subject"], only_html["Message-ID"] = SENDER, SUBJECT, "<h@x>"
    only_html.set_content(html, subtype="html")
    did = run(real, only_html)
    row = db.get_donation(did)
    assert (row["amount"], row["donor"], row["message"]) == ("50.00", "TestPseudo", "TestMessage")


def test_two_donations_in_one_email(real, api):
    cfg, db, _ = real
    two = body(dict(ONE, amount="10", pseudo="P1", message="M1"),
               dict(ONE, amount="15.50", pseudo="P2", streamer="Alice", message=""),
               total="25.50 CHF")
    run(real, make_email(two, subject=SUBJECT, sender=SENDER, msgid="<multi@x>"))
    rows = sorted(db.recent_donations(), key=lambda r: r["id"])
    assert [(r["message_id"], r["donor"], r["amount"], r["message"]) for r in rows] == [
        ("<multi@x>#1", "P1", "10.00", "M1"), ("<multi@x>#2", "P2", "15.50", "")]
    assert db.donations_total() == Decimal("25.50")
    run(real, make_email(two, subject=SUBJECT, sender=SENDER, msgid="<multi@x>"))
    assert len(db.recent_donations()) == 2                 # not processed twice


def test_other_emails_ignored(real, api):
    cfg, db, _ = real
    assert run(real, make_email(body(ONE), subject="Re: question", sender=SENDER)) is None
    assert run(real, make_email(body(ONE), subject=SUBJECT, sender="x@spam.ch")) is None
    assert run(real, make_email("Merci pour votre don", subject=SUBJECT,
                                sender=SENDER)) is None   # no "A Streameur-euses:"
    assert api.calls == []
