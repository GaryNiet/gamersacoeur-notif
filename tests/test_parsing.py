from decimal import Decimal

import pytest

from gamersacoeur_notif.parsing import (email_text, find_streamer, html_to_text, parse_amount,
                                        parse_donation)
from gamersacoeur_notif.streamlabs import sanitize_donor_name

from .conftest import make_email


@pytest.mark.parametrize("raw,expected", [
    ("25.00", "25.00"), ("CHF 10", "10.00"), ("1'000.50", "1000.50"), ("1’000.50", "1000.50"),
    ("1 000,50", "1000.50"), ("12,5", "12.50"), ("1,000", "1000.00"), ("1.000,25", "1000.25"),
    ("1,000.25", "1000.25"), ("25.-", "25.00"), ("50.- CHF", "50.00"),
])
def test_parse_amount(raw, expected):
    assert parse_amount(raw) == Decimal(expected)


@pytest.mark.parametrize("raw", [None, "", "abc", "0", "0.00"])
def test_parse_amount_invalid(raw):
    assert parse_amount(raw) is None


BODY = """Bonjour,

Vous avez reçu un nouveau don via TWINT.

Montant : CHF 1'250.00
Nom : Zoé Müller
Message : Allez Alice, on y croit ! Bisous à Bob
Pour : Alice_TV

Merci !
"""


def test_parse_donation(cfg):
    d = parse_donation("Nouveau don", BODY, cfg.parsing)
    assert d.amount == Decimal("1250.00")
    assert d.currency == "CHF"
    assert d.donor == "Zoé Müller"
    assert d.message == "Allez Alice, on y croit ! Bisous à Bob"
    assert d.streamer_hint == "Alice_TV"
    assert find_streamer(d.streamer_hint, cfg.streamers).slug == "alice"


def test_parse_donation_defaults(cfg):
    d = parse_donation("Don", "Montant: 5 CHF", cfg.parsing)
    assert d.amount == Decimal("5.00")
    assert d.donor == "Anonyme"
    assert d.message == ""
    assert d.streamer_hint is None
    assert find_streamer(d.streamer_hint, cfg.streamers) is None


def test_find_streamer_rules(cfg):
    s = cfg.streamers
    assert find_streamer("pour BOB svp", s).slug == "bob"
    assert find_streamer("bobby", s) is None               # whole words only
    assert find_streamer("ÀLICE", s).slug == "alice"       # accents/case ignored
    assert find_streamer("Bob puis Alice", s).slug == "bob"  # earliest mention wins
    assert find_streamer("gamersacoeur", s).slug == "gamersacoeur"  # slug fallback


def test_find_streamer_whole_email_when_no_rule(cfg):
    cfg.parsing.streamer = None
    d = parse_donation("Don pour bobstream", "Montant: 5", cfg.parsing)
    assert find_streamer(d.streamer_hint, cfg.streamers).slug == "bob"


def test_named_group(cfg):
    cfg.parsing.amount = r"(?P<currency>CHF) (?P<value>\d+)"
    assert parse_donation("", "CHF 42", cfg.parsing).amount == Decimal("42.00")


def test_html_email():
    html = ("<html><style>p{color:red}</style><body><p>Montant&nbsp;: <b>CHF 20.00</b></p>"
            "<table><tr><td>Nom :</td><td>Jean</td></tr></table></body></html>")
    text = html_to_text(html)
    assert "Montant : CHF 20.00" in text
    assert "p{color" not in text


def test_email_text_prefers_plain():
    msg = make_email("plain body", html="<p>html body</p>")
    assert email_text(msg) == "plain body"


def test_email_text_html_only():
    from email.message import EmailMessage
    msg = EmailMessage()
    msg["Subject"] = "x"
    msg.set_content("<p>Montant: CHF 7</p><p>Nom: Léa</p>", subtype="html")
    assert email_text(msg) == "Montant: CHF 7\nNom: Léa"


@pytest.mark.parametrize("raw,expected", [
    ("Zoé Müller", "Zoe Muller"), ("A", "Anonyme"), ("", "Anonyme"),
    ("jean.dupont@mail.ch", "jean_dupont_mail_ch"), ("x" * 40, "x" * 25), ("李", "Anonyme"),
])
def test_sanitize_donor_name(raw, expected):
    assert sanitize_donor_name(raw) == expected
