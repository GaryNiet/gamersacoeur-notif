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


def login(client, monkeypatch, user=("42", ["Alice_TV", "alice"]), token="AT"):
    """Open the shared link and log in with Streamlabs; returns the final page text."""
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


def test_anyone_can_sign_up(client, db, monkeypatch):
    text = login(client, monkeypatch)
    assert "Alice_TV" in text and "tout bon" in text
    [st] = db.streamers()
    assert st.name == "Alice_TV" and st.keywords == ("Alice_TV", "alice") and not st.main
    assert db.get_token(st.id)["access_token"] == "AT"


def test_main_account_from_config(client, db, monkeypatch):
    login(client, monkeypatch, user=("1", ["GamersACoeur"]))
    assert [s.name for s in db.main_streamers()] == ["GamersACoeur"]


def test_relogin_keeps_admin_changes(client, db, monkeypatch):
    login(client, monkeypatch, user=("7", ["Bob"]))
    [st] = db.streamers()
    db.update_streamer(st.id, keywords=["bobby"], main=True, enabled=True)
    text = login(client, monkeypatch, user=("7", ["Bob"]), token="AT2")
    assert "bobby" in text
    [st2] = db.streamers()
    assert st2.id == st.id and st2.keywords == ("bobby",) and st2.main
    assert db.get_token(st.id)["access_token"] == "AT2"


def test_approval_mode(client, cfg, db, monkeypatch):
    cfg.new_streamers_enabled = False
    assert "activer" in login(client, monkeypatch)
    assert db.streamers() == []                       # not routed until enabled
    assert len(db.streamer_rows()) == 1


def test_user_lookup_failure(client, db, monkeypatch):
    text = login(client, monkeypatch, user=(None, []))
    assert "préviens l'organisation" in text
    [row] = db.streamer_rows()
    assert row["streamlabs_id"].startswith("token:") and row["name"] == "(nom inconnu)"


def test_admin_edit_streamer(client, db, monkeypatch):
    login(client, monkeypatch)
    [st] = db.streamers()
    r = client.post(f"/admin/streamer/{st.id}", headers=AUTH,
                    data={"csrf": csrf_of(client), "keywords": " alice , alicia,, ", "main": "on",
                          "enabled": "on"})
    assert r.status_code == 302
    [st] = db.streamers()
    assert st.keywords == ("alice", "alicia") and st.main
    client.post(f"/admin/streamer/{st.id}", headers=AUTH,
                data={"csrf": csrf_of(client), "keywords": "alice"})   # boxes unticked
    assert db.streamers() == [] and db.main_streamers() == []


def test_admin_delete_streamer(client, db, monkeypatch):
    login(client, monkeypatch)
    [st] = db.streamers()
    client.post(f"/admin/streamer/{st.id}/delete", headers=AUTH, data={"csrf": csrf_of(client)})
    assert db.streamer_rows() == []


def test_admin_page_escapes_names(client, db, monkeypatch):
    evil = "x');alert(1);('\"<b>"
    login(client, monkeypatch, user=("666", [evil]))
    page = client.get("/admin", headers=AUTH).get_data(as_text=True)
    assert "<b>" not in page and "');alert(1)" not in page
    assert "Aucun compte principal" in page


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


def test_admin_test_button_and_csrf(client, db, api, people):
    bob = people["bob"].id
    assert client.post(f"/admin/test/{bob}", headers=AUTH, data={"csrf": "bad"}).status_code == 400
    assert client.post(f"/admin/test/{bob}", headers=AUTH,
                       data={"csrf": csrf_of(client)}).status_code == 302
    assert api.calls[0][1] == "tok-bob"


def test_get_user_parses_names(monkeypatch):
    class R:
        def raise_for_status(self): pass
        def json(self):
            return {"streamlabs": {"id": 123, "display_name": "GaC"},
                    "twitch": {"id": 9, "name": "gac_twitch", "display_name": "GaC_Twitch"}}
    monkeypatch.setattr(streamlabs.requests, "get", lambda *a, **k: R())
    assert streamlabs.get_user("t") == ("123", ["GaC", "GaC_Twitch", "gac_twitch"])


def test_donations_csv_export(client, db):
    bob = db.add_test_streamer("Bob", ["bob"])
    d = db.add_donation(message_id="m1", subject="Nouveau don", donor="=HYPERLINK(1)",
                        amount="12.50", currency="CHF", message="Allez; Bob !",
                        routed_to=bob.id, status="sent")
    db.add_delivery(d, bob.id, "sent")
    assert client.get("/admin/donations.csv").status_code == 401
    r = client.get("/admin/donations.csv", headers=AUTH)
    assert r.status_code == 200 and r.mimetype == "text/csv"
    assert "attachment" in r.headers["Content-Disposition"]
    import csv, io
    rows = list(csv.reader(io.StringIO(r.get_data(as_text=True).lstrip("﻿")), delimiter=";"))
    assert rows[0][:4] == ["id", "recu_utc", "donateur", "montant"]
    assert rows[1][2] == "'=HYPERLINK(1)"          # can't run as a formula in Excel
    assert rows[1][3:7] == ["12.50", "CHF", "Allez; Bob !", "Bob"]
    assert rows[1][8] == "Bob: sent"
    assert "Exporter en CSV" in client.get("/admin", headers=AUTH).get_data(as_text=True)


def test_email_fields_in_admin_and_csv(client, db):
    db.add_donation(message_id="m2", subject="S", donor="P", amount="5.00", currency="CHF",
                    message="", routed_to=[], status="sent",
                    fields={"Donateur": "Jean Dupont", "Email": "j@example.ch"},
                    email_text="Donateur: Jean Dupont\nEmail: j@example.ch")
    page = client.get("/admin", headers=AUTH).get_data(as_text=True)
    assert "Jean Dupont" in page and "Détails de l'email" in page
    import csv, io
    text = client.get("/admin/donations.csv", headers=AUTH).get_data(as_text=True)
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿")), delimiter=";"))
    h = rows[0]
    assert h[-2:] == ["email: Donateur", "email: Email"]      # no full email text
    assert rows[1][h.index("email: Donateur")] == "Jean Dupont"
    assert len(rows[1]) == len(h)
