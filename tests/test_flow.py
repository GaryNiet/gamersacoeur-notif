from decimal import Decimal

import pytest

from gamersacoeur_notif.config import ConfigError, config_from_dict
from gamersacoeur_notif.streamlabs import StreamlabsError
from gamersacoeur_notif.worker import process_message

from .conftest import make_email

BODY = "Montant : CHF 20.00\nNom : Jean\nMessage : Courage !\nPour : Bob\n"


def tokens(api):
    return [c[1] for c in api.calls]


def process(cfg, db, notifier, msg):
    """process_message + wait for the background send queues to finish."""
    did = process_message(cfg, db, notifier, msg)
    notifier.pacer.wait_idle()
    return did


def test_routed_donation_goes_to_streamer_and_main(cfg, db, notifier, api, people):
    did = process(cfg, db, notifier, make_email(BODY))
    assert tokens(api) == ["tok-bob", "tok-main"]
    kw = api.calls[0][2]
    assert kw["amount"] == Decimal("20.00") and kw["currency"] == "CHF"
    assert kw["name"] == "Jean" and kw["message"] == "Courage !"
    row = db.get_donation(did)
    assert row["status"] == "sent" and row["routed_to"] == people["bob"].id


def test_unmatched_goes_to_main_only(cfg, db, notifier, api, people):
    process(cfg, db, notifier, make_email("Montant : CHF 5\nPour : personne"))
    assert tokens(api) == ["tok-main"]


def test_disabled_streamer_not_routed(cfg, db, notifier, api, people):
    db.update_streamer(people["bob"].id, keywords=["bob"], main=False, enabled=False)
    process(cfg, db, notifier, make_email(BODY))
    assert tokens(api) == ["tok-main"]


def test_edited_keywords_used(cfg, db, notifier, api, people):
    db.update_streamer(people["alice"].id, keywords=["bob"], main=False, enabled=True)
    db.update_streamer(people["bob"].id, keywords=["robert"], main=False, enabled=True)
    process(cfg, db, notifier, make_email(BODY))
    assert tokens(api) == ["tok-alice", "tok-main"]


def test_several_main_accounts(cfg, db, notifier, api, people):
    db.update_streamer(people["alice"].id, keywords=["alice"], main=True, enabled=True)
    process(cfg, db, notifier, make_email(BODY))
    assert tokens(api) == ["tok-bob", "tok-main", "tok-alice"]


def test_duplicate_email_ignored(cfg, db, notifier, api, people):
    assert process(cfg, db, notifier, make_email(BODY)) is not None
    assert process(cfg, db, notifier, make_email(BODY)) is None
    assert tokens(api).count("tok-main") == 1


def test_filters(cfg, db, notifier, api, people):
    assert process(cfg, db, notifier, make_email(BODY, sender="spam@x.com")) is None
    assert process(cfg, db, notifier, make_email(BODY, subject="Newsletter")) is None
    assert api.calls == []


def test_unparsed_amount_not_sent(cfg, db, notifier, api, people):
    did = process(cfg, db, notifier, make_email("Pas de montant ici"))
    assert db.get_donation(did)["status"] == "unparsed"
    assert api.calls == []


def test_error_then_retry_only_missing(cfg, db, notifier, api, people):
    api.fail_tokens["tok-bob"] = StreamlabsError("boom", 500)
    did = process(cfg, db, notifier, make_email(BODY))
    assert db.get_donation(did)["status"] == "partial"
    del api.fail_tokens["tok-bob"]
    assert notifier.retry(did) == ["Bob"]                   # main is not re-sent
    notifier.pacer.wait_idle()
    assert db.get_donation(did)["status"] == "sent"
    assert notifier.retry(did) == []
    assert tokens(api) == ["tok-main", "tok-bob"]


def test_deleted_streamer(cfg, db, notifier, api, people):
    db.delete_streamer(people["main"].id)
    process(cfg, db, notifier, make_email(BODY))
    assert tokens(api) == ["tok-bob"]


def test_token_refresh_on_401(cfg, db, notifier, api):
    main = db.add_test_streamer("Main", main=True, token="old")
    db.update_tokens(main.id, "old", "r1")
    api.fail_tokens["old"] = StreamlabsError("unauthorized", 401)
    process(cfg, db, notifier, make_email("Montant: 3"))
    assert tokens(api) == ["new-r1"]
    assert db.get_token(main.id)["access_token"] == "new-r1"


def test_error_marks_failed(cfg, db, notifier, api, people):
    api.fail_tokens["tok-main"] = StreamlabsError("boom", 500)
    did = process(cfg, db, notifier, make_email("Montant: 3"))
    assert db.get_donation(did)["status"] == "failed"


def test_alert_mode(cfg, db, notifier, api, people):
    cfg.streamlabs.mode = "alert"
    process(cfg, db, notifier, make_email(BODY))
    kind, _, kw = api.calls[0]
    assert kind == "alert"
    assert kw["message"] == "*Jean* a donné *20.00 CHF* via TWINT !"
    assert kw["user_message"] == "Courage !"


def test_dry_run(cfg, db, notifier, api, people):
    cfg.streamlabs.dry_run = True
    did = process(cfg, db, notifier, make_email(BODY))
    assert api.calls == []
    assert {d["status"] for d in db.deliveries_for(did)} == {"dry_run"}
    assert db.get_donation(did)["status"] == "sent"


def test_main_streamer_routed_not_duplicated(notifier, people):
    assert notifier.targets(people["main"]) == [people["main"]]


def test_config_rejects_old_streamers_list(raw_cfg):
    raw_cfg["streamers"] = [{"slug": "a", "name": "A"}]
    with pytest.raises(ConfigError):
        config_from_dict(raw_cfg)


def test_config_main_accounts(cfg):
    assert cfg.is_main_account(["foo", "gamersacoeur"])
    assert not cfg.is_main_account(["foo"])


def test_numeric_secrets_in_yaml(raw_cfg):
    """`secret_key: 123456` in YAML is a number: it must still work as text."""
    raw_cfg["secret_key"] = 123456
    raw_cfg["admin_password"] = 2026
    raw_cfg["mail"]["password"] = 1234
    raw_cfg["streamlabs"]["client_id"] = 4242
    raw_cfg["progress_bar"] = {"ftp": {"host": "h", "username": "u", "password": 99}}
    cfg = config_from_dict(raw_cfg)
    assert cfg.secret_key == "123456" and cfg.admin_password == "2026"
    assert cfg.mail.password == "1234" and cfg.streamlabs.client_id == "4242"
    assert cfg.progress_bar.ftp.password == "99"
    assert cfg.port == 8000 and cfg.mail.port == 993          # real numbers untouched


def test_home_page_with_numeric_secret_key(raw_cfg):
    from gamersacoeur_notif.db import Database
    from gamersacoeur_notif.web import create_app

    raw_cfg["secret_key"] = 123456
    cfg = config_from_dict(raw_cfg)
    app = create_app(cfg, Database(cfg.database))
    assert app.test_client().get("/").status_code == 200
