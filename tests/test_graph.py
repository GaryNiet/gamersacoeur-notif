import pytest

from gamersacoeur_notif.config import MailConfig
from gamersacoeur_notif.graph import GraphError, GraphMailbox

from .conftest import make_email


class Resp:
    def __init__(self, status=200, data=None, content=b""):
        self.status_code, self._data, self.content = status, data, content
        self.text = str(data)

    def json(self):
        return self._data


class FakeGraph:
    """Minimal stand-in for login.microsoftonline.com + graph.microsoft.com."""

    def __init__(self):
        self.messages = []          # (receivedDateTime, id, EmailMessage)
        self.folders = {"Dons": "FOLDER-ID"}
        self.tokens_issued = 0
        self.expire_next = False
        self.page_size = 50
        self.gets = []

    def post(self, url, data, timeout):
        assert "/oauth2/v2.0/token" in url and data["grant_type"] == "client_credentials"
        assert data["scope"] == "https://graph.microsoft.com/.default"
        if data["client_secret"] != "secret":
            return Resp(401, {"error": "invalid_client"})
        self.tokens_issued += 1
        return Resp(200, {"access_token": f"T{self.tokens_issued}", "expires_in": 3600})

    def get(self, url, params=None, headers=None, timeout=None):
        self.gets.append(url)
        if self.expire_next:
            self.expire_next = False
            return Resp(401, {"error": "expired"})
        assert headers["Authorization"].startswith("Bearer T")
        if url.endswith("/$value"):
            mid = url.split("/messages/")[1].split("/")[0]
            msg = next(m for _, i, m in self.messages if i == mid)
            return Resp(200, content=msg.as_bytes())
        if "childFolders" in url or url.endswith("/mailFolders"):
            name = params["$filter"].split("'")[1]
            fid = self.folders.get(name) if url.endswith("/mailFolders") else None
            return Resp(200, {"value": [{"id": fid}] if fid else []})
        if url.endswith("/messages") and params and "$filter" not in params:
            newest = sorted(((r, i) for r, i, _ in self.messages), reverse=True)
            return Resp(200, {"value": [{"id": i} for _, i in newest[:int(params["$top"])]]})
        if url.endswith("/messages") or "page=" in url:
            if params:
                since = params["$filter"].split(" ge ")[1]
                found = sorted((r, i) for r, i, _ in self.messages if r >= since)
                self._pending = found
            page, self._pending = self._pending[:self.page_size], self._pending[self.page_size:]
            data = {"value": [{"receivedDateTime": r, "id": i} for r, i in page]}
            if self._pending:
                data["@odata.nextLink"] = "https://graph.microsoft.com/v1.0/next?page=2"
            return Resp(200, data)
        return Resp(404, {"error": url})

    def add(self, received, mid, body="Don: 5 CHF"):
        self.messages.append((received, mid, make_email(body, msgid=f"<{mid}@x>")))


def mailcfg(**kw):
    base = dict(provider="graph", tenant_id="t", client_id="c", client_secret="secret",
                mailbox="dons@example.ch", folder="inbox")
    base.update(kw)
    return MailConfig(**base)


def run(box):
    got = box.fetch_new()
    for key, _ in got:
        box.mark_done(key)
    return [m["Message-ID"] for _, m in got]


def test_existing_mail_ignored_then_new_fetched(db, monkeypatch):
    g = FakeGraph()
    g.add("2020-01-01T10:00:00Z", "old")
    monkeypatch.setattr("gamersacoeur_notif.graph._utc_now", lambda: "2026-11-25T10:00:00Z")
    box = GraphMailbox(mailcfg(), db, http=g)
    box.connect()
    assert run(box) == []
    g.add("2026-11-25T10:00:05Z", "new1")
    g.add("2026-11-25T10:00:05Z", "new2")       # same second
    assert run(box) == ["<new1@x>", "<new2@x>"]
    assert run(box) == []
    g.add("2026-11-25T10:00:05Z", "late")       # arrives later with the same timestamp
    assert run(box) == ["<late@x>"]


def test_position_survives_restart(db):
    g = FakeGraph()
    box = GraphMailbox(mailcfg(process_existing=True), db, http=g)
    g.add("2026-01-01T00:00:00Z", "a")
    box.connect()
    assert run(box) == ["<a@x>"]
    g.add("2026-01-01T00:00:01Z", "b")
    box2 = GraphMailbox(mailcfg(process_existing=True), db, http=g)
    box2.connect()
    assert run(box2) == ["<b@x>"]


def test_unprocessed_message_is_retried(db):
    g = FakeGraph()
    box = GraphMailbox(mailcfg(process_existing=True), db, http=g)
    g.add("2026-01-01T00:00:00Z", "a")
    box.connect()
    assert len(box.fetch_new()) == 1     # crash before mark_done...
    assert run(box) == ["<a@x>"]         # ...so it comes back


def test_paging(db):
    g = FakeGraph()
    g.page_size = 2
    for i in range(5):
        g.add(f"2026-01-01T00:00:0{i}Z", f"m{i}")
    box = GraphMailbox(mailcfg(process_existing=True), db, http=g)
    box.connect()
    assert run(box) == [f"<m{i}@x>" for i in range(5)]


def test_named_folder(db):
    g = FakeGraph()
    box = GraphMailbox(mailcfg(folder="Dons"), db, http=g)
    box.connect()
    box.fetch_new()
    assert "/mailFolders/FOLDER-ID/messages" in g.gets[-1]
    with pytest.raises(GraphError, match="not found"):
        GraphMailbox(mailcfg(folder="Nope"), db, http=g).connect()


def test_token_reused_and_refreshed_on_401(db):
    g = FakeGraph()
    box = GraphMailbox(mailcfg(), db, http=g)
    box.connect()
    box.fetch_new()
    box.fetch_new()
    assert g.tokens_issued == 1
    g.expire_next = True
    box.fetch_new()
    assert g.tokens_issued == 2


def test_bad_secret(db):
    with pytest.raises(GraphError, match="login failed"):
        GraphMailbox(mailcfg(client_secret="wrong"), db, http=FakeGraph()).connect()


def test_check_mail_command(tmp_path, monkeypatch, capsys, raw_cfg):
    import yaml

    import gamersacoeur_notif.__main__ as cli

    raw_cfg["mail"] = dict(provider="graph", tenant_id="t", client_id="c",
                           client_secret="secret", mailbox="dons@example.ch",
                           body_contains=["Don:"])
    raw_cfg["parsing"] = {"amount": r"^Don:[ \t]*([^\r\n]+)",
                          "streamer": r"^A Streameur-euses:[ \t]*([^\r\n]+)"}
    path = tmp_path / "c.yaml"
    path.write_text(yaml.safe_dump(raw_cfg, allow_unicode=True), encoding="utf-8")
    g = FakeGraph()
    g.add("2026-01-01T00:00:00Z", "news", body="Newsletter")
    g.add("2026-01-01T00:00:01Z", "don", body="A Streameur-euses: Bob\nDon: 20 CHF")
    monkeypatch.setattr(cli, "open_mailbox", lambda cfg, db: GraphMailbox(cfg, db, http=g))

    assert cli.main(["-c", str(path), "check-mail"]) == 0
    out = capsys.readouterr().out
    assert "OK - connected" in out
    assert "Amount   : 20.00 CHF" in out and "For      : Bob" in out
    assert "IGNORED" in out
    assert out.index("20.00 CHF") < out.index("IGNORED")       # newest first

    raw_cfg["mail"]["client_secret"] = "wrong"
    path.write_text(yaml.safe_dump(raw_cfg, allow_unicode=True), encoding="utf-8")
    assert cli.main(["-c", str(path), "check-mail"]) == 1
    assert "Microsoft login failed" in capsys.readouterr().err
