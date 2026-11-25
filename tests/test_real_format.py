"""The real donation email format (see config.example.yaml)."""
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from gamersacoeur_notif.config import ConfigError, ParsingConfig, config_from_dict
from gamersacoeur_notif.mailbox import matches_filters
from gamersacoeur_notif.parsing import find_streamer, parse_donation

from .conftest import make_email

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def example():
    raw = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    return raw


@pytest.fixture
def rules(example):
    return ParsingConfig(**example["parsing"])


EMAIL = """Nouveau don reçu

Donateur: Jean, Dupont
Pseudo: JeanTheGamer
A Streameur-euses: Alice_TV
Don: 1'250.50 CHF
Date: 25.11.2026 20:14
Message: Allez Alice !
Postal Address: Rue du Lac 1, 1000 Lausanne
Email: jean@example.ch
"""


def test_same_line_message(rules):
    d = parse_donation("Don", EMAIL, rules)
    assert d.amount == Decimal("1250.50")
    assert d.donor == "JeanTheGamer"
    assert d.message == "Allez Alice !"
    assert d.streamer_hint == "Alice_TV"


def test_message_on_next_line(rules):
    body = EMAIL.replace("Message: Allez Alice !", "Message:\nAllez Alice !")
    assert parse_donation("Don", body, rules).message == "Allez Alice !"


def test_empty_message_and_pseudo(rules):
    body = EMAIL.replace("Message: Allez Alice !", "Message:").replace(
        "Pseudo: JeanTheGamer", "Pseudo:")
    d = parse_donation("Don", body, rules)
    assert d.message == ""                 # must not grab "Postal Address: ..."
    assert d.donor == "Anonyme"            # must not grab the next line
    assert d.streamer_hint == "Alice_TV"


def test_donor_fallback_list(rules):
    rules.donor = [r"^Pseudo:[ \t]*([^\r\n]+)", r"^Donateur:[ \t]*([^,\r\n]+)"]
    body = EMAIL.replace("Pseudo: JeanTheGamer", "Pseudo:")
    assert parse_donation("Don", body, rules).donor == "Jean"


def test_routing_on_real_format(rules, people):
    body = EMAIL.replace("Alice_TV", "Bob, Alice")
    d = parse_donation("Don", body, rules)
    assert find_streamer(d.streamer_hint, list(people.values())) == people["bob"]


def test_body_filter(example, monkeypatch):
    monkeypatch.setenv("MS_CLIENT_SECRET", "x")
    monkeypatch.setenv("FTP_PASSWORD", "x")
    for k in ("GAC_SECRET_KEY", "GAC_ADMIN_PASSWORD", "STREAMLABS_CLIENT_ID",
              "STREAMLABS_CLIENT_SECRET"):
        monkeypatch.setenv(k, "x")
    from gamersacoeur_notif.config import _expand_env
    cfg = config_from_dict(_expand_env(example))
    assert cfg.mail.provider == "graph"
    sender, subject = "Chaîne du Bonheur <info@bonheur.ch>", "[iRaiser] notification #1 : CAC"
    assert matches_filters(make_email(EMAIL, sender=sender, subject=subject), cfg.mail)
    assert not matches_filters(make_email("Newsletter", sender=sender, subject=subject), cfg.mail)
    assert not matches_filters(make_email(EMAIL, sender="x@y.ch", subject=subject), cfg.mail)


def test_provider_validation(raw_cfg):
    raw_cfg["mail"] = {"provider": "graph", "mailbox": "a@b.ch"}
    with pytest.raises(ConfigError, match="tenant_id"):
        config_from_dict(raw_cfg)
    raw_cfg["mail"] = {"provider": "pop3"}
    with pytest.raises(ConfigError):
        config_from_dict(raw_cfg)
