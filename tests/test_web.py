import base64
import re
from urllib.parse import parse_qs, urlsplit

import pytest

from gamersacoeur_notif import streamlabs
from gamersacoeur_notif.web import connect_link, connect_signature, create_app

AUTH = {"Authorization": "Basic " + base64.b64encode(b"admin:adminpw").decode()}


@pytest.fixture
def client(cfg, db, notifier):
    app = create_app(cfg, db, notifier)
    app.testing = True
    return app.test_client()


def test_connect_link_uses_redirect_base(cfg):
    sig = connect_signature("test-secret", "alice")
    assert connect_link(cfg, "alice") == f"https://notif.example.ch/connect/alice/{sig}"


def test_connect_bad_signature(client):
    assert client.get("/connect/alice/wrong").status_code == 404
    assert client.get("/connect/nobody/" + connect_signature("test-secret", "nobody")).status_code == 404


def test_oauth_flow(client, db, monkeypatch):
    sig = connect_signature("test-secret", "alice")
    r = client.get(f"/connect/alice/{sig}")
    assert r.status_code == 200
    url = re.search(r'href="([^"]+)"', r.get_data(as_text=True)).group(1).replace("&amp;", "&")
    q = parse_qs(urlsplit(url).query)
    assert q["client_id"] == ["cid"] and "donations.create" in q["scope"][0]

    monkeypatch.setattr(streamlabs, "exchange_code",
                        lambda *a: {"access_token": "AT", "refresh_token": "RT"})
    monkeypatch.setattr(streamlabs, "get_user_name", lambda t: "AliceTV")
    r = client.get(f"/callback?code=xyz&state={q['state'][0]}")
    assert r.status_code == 200 and "connecté" in r.get_data(as_text=True)
    tok = db.get_token("alice")
    assert tok["access_token"] == "AT" and tok["streamlabs_name"] == "AliceTV"


def test_callback_rejects_bad_state(client):
    sig = connect_signature("test-secret", "alice")
    client.get(f"/connect/alice/{sig}")
    assert client.get("/callback?code=xyz&state=forged").status_code == 400


def test_admin_requires_password(client):
    assert client.get("/admin").status_code == 401
    bad = {"Authorization": "Basic " + base64.b64encode(b"admin:nope").decode()}
    assert client.get("/admin", headers=bad).status_code == 401
    r = client.get("/admin", headers=AUTH)
    assert r.status_code == 200 and "Alice" in r.get_data(as_text=True)


def test_admin_test_button_and_csrf(client, db, api):
    db.save_token("bob", "tok-bob")
    page = client.get("/admin", headers=AUTH).get_data(as_text=True)
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    assert client.post("/admin/test/bob", headers=AUTH, data={"csrf": "bad"}).status_code == 400
    r = client.post("/admin/test/bob", headers=AUTH, data={"csrf": csrf})
    assert r.status_code == 302
    assert api.calls[0][1] == "tok-bob"
