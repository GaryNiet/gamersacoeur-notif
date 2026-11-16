from decimal import Decimal

import pytest

from gamersacoeur_notif.config import ConfigError, config_from_dict
from gamersacoeur_notif.streamlabs import StreamlabsError
from gamersacoeur_notif.worker import process_message

from .conftest import make_email

BODY = "Montant : CHF 20.00\nNom : Jean\nMessage : Courage !\nPour : Bob\n"


def test_routed_donation_goes_to_streamer_and_main(cfg, db, notifier, api):
    db.save_token("bob", "tok-bob")
    db.save_token("gamersacoeur", "tok-main")
    did = process_message(cfg, db, notifier, make_email(BODY))
    assert [(c[0], c[1]) for c in api.calls] == [("donation", "tok-bob"), ("donation", "tok-main")]
    kw = api.calls[0][2]
    assert kw["amount"] == Decimal("20.00") and kw["currency"] == "CHF"
    assert kw["name"] == "Jean" and kw["message"] == "Courage !"
    row = db.get_donation(did)
    assert row["status"] == "sent" and row["routed_to"] == "bob"


def test_unmatched_goes_to_main_only(cfg, db, notifier, api):
    db.save_token("gamersacoeur", "tok-main")
    process_message(cfg, db, notifier, make_email("Montant : CHF 5\nPour : personne"))
    assert [c[1] for c in api.calls] == ["tok-main"]


def test_duplicate_email_ignored(cfg, db, notifier, api):
    db.save_token("gamersacoeur", "tok-main")
    assert process_message(cfg, db, notifier, make_email(BODY)) is not None
    assert process_message(cfg, db, notifier, make_email(BODY)) is None
    assert len([c for c in api.calls if c[1] == "tok-main"]) == 1


def test_filters(cfg, db, notifier, api):
    assert process_message(cfg, db, notifier, make_email(BODY, sender="spam@x.com")) is None
    assert process_message(cfg, db, notifier, make_email(BODY, subject="Newsletter")) is None
    assert api.calls == []


def test_unparsed_amount_not_sent(cfg, db, notifier, api):
    db.save_token("gamersacoeur", "tok-main")
    did = process_message(cfg, db, notifier, make_email("Pas de montant ici"))
    assert db.get_donation(did)["status"] == "unparsed"
    assert api.calls == []


def test_not_connected_then_retry(cfg, db, notifier, api):
    db.save_token("gamersacoeur", "tok-main")
    did = process_message(cfg, db, notifier, make_email(BODY))
    assert db.get_donation(did)["status"] == "partial"   # bob not connected yet
    db.save_token("bob", "tok-bob")
    assert notifier.retry(did) == {"bob": "sent"}           # main is not re-sent
    assert db.get_donation(did)["status"] == "sent"
    assert [c[1] for c in api.calls] == ["tok-main", "tok-bob"]


def test_token_refresh_on_401(cfg, db, notifier, api):
    db.save_token("gamersacoeur", "old", refresh_token="r1")
    api.fail_tokens["old"] = StreamlabsError("unauthorized", 401)
    process_message(cfg, db, notifier, make_email("Montant: 3"))
    assert api.calls[0][1] == "new-r1"
    assert db.get_token("gamersacoeur")["access_token"] == "new-r1"


def test_error_marks_failed(cfg, db, notifier, api):
    db.save_token("gamersacoeur", "bad")
    api.fail_tokens["bad"] = StreamlabsError("boom", 500)
    did = process_message(cfg, db, notifier, make_email("Montant: 3"))
    assert db.get_donation(did)["status"] == "failed"


def test_alert_mode(cfg, db, notifier, api):
    cfg.streamlabs.mode = "alert"
    db.save_token("gamersacoeur", "tok-main")
    process_message(cfg, db, notifier, make_email(BODY))
    kind, _, kw = api.calls[0]
    assert kind == "alert"
    assert kw["message"] == "*Jean* a donné *20.00 CHF* via TWINT !"
    assert kw["user_message"] == "Courage !"


def test_dry_run(cfg, db, notifier, api):
    cfg.streamlabs.dry_run = True
    did = process_message(cfg, db, notifier, make_email(BODY))
    assert api.calls == []
    assert {d["status"] for d in db.deliveries_for(did)} == {"dry_run"}
    assert db.get_donation(did)["status"] == "sent"


def test_main_streamer_routed_not_duplicated(cfg, notifier):
    main = cfg.streamer("gamersacoeur")
    assert notifier.targets(main) == [main]


def test_config_requires_main(raw_cfg):
    for s in raw_cfg["streamers"]:
        s["main"] = False
    with pytest.raises(ConfigError):
        config_from_dict(raw_cfg)


def test_config_bad_slug(raw_cfg):
    raw_cfg["streamers"][1]["slug"] = "Alice B"
    with pytest.raises(ConfigError):
        config_from_dict(raw_cfg)
