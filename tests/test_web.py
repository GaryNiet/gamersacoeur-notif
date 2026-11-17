import base64
import re
from urllib.parse import parse_qs, urlsplit

import pytest

from gamersacoeur_notif import streamlabs
from gamersacoeur_notif.web import connect_link, create_app

AUTH = {"Authorization": "Basic " + base64.b64encode(b"admin:adminpw").decode()}


@pytest.fixture
def client(cfg, db, notifier):
    app = create_app(cfg, db, notifier)
    app.testing = True
    return app.test_client()


def login(client, monkeypatch, user=("42", ["alice_tv"]), token="AT"):
    """Go through the shared link + Streamlabs login; returns the final page text."""
    page = client.get("/").get_data(as_text=True)
    url = re.search(r'href="([^"]+)"', page).group(1).replace("&amp;", "&")
    q = parse_qs(urlsplit(url).query)
    assert q["client_id"] == ["cid"] and "donations.create" in q["scope"][0]
    monkeypatch.setattr(streamlabs, "exchange_code",
                        lambda *a: {"access_token": token, "refresh_token": "RT"})
    monkeypatch.setattr(streamlabs, "get_user", lambda t: user)
    r = client.get(f"/callback?code=xyz&state={q['state'][0]}")
    assert r.status_code == 200
    return r.get_data(as_text=True)


def csrf_of(client):
    page = client.get("/admin", headers=AUTH).get_data(as_text=True)
    return re.search(r'name="csrf" value="([^"]+)"', page).group(1)


def test_single_link(cfg):
    assert connect_link(cfg) == "https://notif.example.ch/"


def test_login_matched_by_streamlabs_name(client, db, monkeypatch):
    text = login(client, monkeypatch, user=("42", ["Some Twitch Name", "ALICE_TV"]))
    assert "Merci Alice" in text
    acc = db.get_token("alice")
    assert acc["access_token"] == "AT" and acc["streamlabs_id"] == "42"
    assert db.unassigned_accounts() == []


def test_relogin_updates_token_and_keeps_link(client, db, monkeypatch):
    login(client, monkeypatch, user=("7", ["Nobody"]))
    acc = db.unassigned_accounts()[0]
    db.assign_account(acc["id"], "bob")
    text = login(client, monkeypatch, user=("7", ["Nobody"]), token="AT2")
    assert "Merci Bob" in text                         # assignment kept on reconnect
    assert db.get_token("bob")["access_token"] == "AT2"


def test_unknown_account_waits_for_admin(client, db, monkeypatch, api):
    text = login(client, monkeypatch, user=("99", ["RandomViewer"]))
    assert "organisation" in text
    assert db.all_tokens() == {}
    [acc] = db.unassigned_accounts()
    assert acc["streamlabs_name"] == "RandomViewer"

    page = client.get("/admin", headers=AUTH).get_data(as_text=True)
    assert "Comptes à attribuer" in page and "RandomViewer" in page
    r = client.post(f"/admin/assign/{acc['id']}", headers=AUTH,
                    data={"csrf": csrf_of(client), "slug": "bob"})
    assert r.status_code == 302
    assert db.get_token("bob")["streamlabs_name"] == "RandomViewer"


def test_user_lookup_failure_still_stores_account(client, db, monkeypatch):
    login(client, monkeypatch, user=(None, []))
    [acc] = db.unassigned_accounts()
    assert acc["streamlabs_id"].startswith("token:") and acc["streamlabs_name"] is None


def test_assign_replaces_previous_account(client, db, monkeypatch):
    login(client, monkeypatch, user=("1", ["alice_tv"]), token="OLD")
    login(client, monkeypatch, user=("2", ["other"]), token="NEW")
    new = db.unassigned_accounts()[0]
    client.post(f"/admin/assign/{new['id']}", headers=AUTH,
                data={"csrf": csrf_of(client), "slug": "alice"})
    assert db.get_token("alice")["access_token"] == "NEW"
    assert [a["access_token"] for a in db.unassigned_accounts()] == ["OLD"]


def test_forget_and_disconnect(client, db, monkeypatch):
    login(client, monkeypatch, user=("5", ["x"]))
    acc = db.unassigned_accounts()[0]
    client.post(f"/admin/forget/{acc['id']}", headers=AUTH, data={"csrf": csrf_of(client)})
    assert db.unassigned_accounts() == []
    login(client, monkeypatch, user=("6", ["BobStream"]))
    client.post("/admin/disconnect/bob", headers=AUTH, data={"csrf": csrf_of(client)})
    assert db.get_token("bob") is None


def test_callback_rejects_bad_state(client):
    client.get("/")
    assert client.get("/callback?code=xyz&state=forged").status_code == 400
    assert client.get("/callback?code=xyz&state=").status_code == 400


def test_admin_requires_password(client):
    assert client.get("/admin").status_code == 401
    bad = {"Authorization": "Basic " + base64.b64encode(b"admin:nope").decode()}
    assert client.get("/admin", headers=bad).status_code == 401
    r = client.get("/admin", headers=AUTH)
    assert r.status_code == 200 and "https://notif.example.ch/" in r.get_data(as_text=True)


def test_admin_test_button_and_csrf(client, db, api):
    db.save_token("bob", "tok-bob")
    csrf = csrf_of(client)
    assert client.post("/admin/test/bob", headers=AUTH, data={"csrf": "bad"}).status_code == 400
    assert client.post("/admin/test/bob", headers=AUTH, data={"csrf": csrf}).status_code == 302
    assert api.calls[0][1] == "tok-bob"


def test_get_user_parses_names(monkeypatch):
    class R:
        def raise_for_status(self): pass
        def json(self):
            return {"streamlabs": {"id": 123, "display_name": "GaC"},
                    "twitch": {"id": 9, "name": "gac_twitch", "display_name": "GaC_Twitch"}}
    monkeypatch.setattr(streamlabs.requests, "get", lambda *a, **k: R())
    assert streamlabs.get_user("t") == ("123", ["GaC", "GaC_Twitch", "gac_twitch"])
