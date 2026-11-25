import datetime
import ftplib
import json
import threading

import pytest

from gamersacoeur_notif.config import FtpConfig, ProgressConfig
from gamersacoeur_notif.progress import ProgressBar
from gamersacoeur_notif.worker import process_message

from .conftest import make_email


class Resp:
    def __init__(self, data=None, status=200):
        self._data, self.status_code = data, status

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(str(self.status_code))

    def json(self):
        return self._data


class FakeCharity:
    def __init__(self, cents=123450):
        self.cents, self.fail, self.calls = cents, False, 0

    def get(self, url, timeout):
        self.calls += 1
        if self.fail:
            import requests
            raise requests.ConnectionError("down")
        return Resp({"amount_raised": self.cents})


class FakeFTP:
    def __init__(self, store, fail=False):
        self.store, self.fail = store, fail

    def __call__(self):            # used as ftp_factory
        if self.fail:
            raise ftplib.error_perm("530 Login incorrect")
        return self

    def cwd(self, d):
        self.store["cwd"] = d

    def storbinary(self, cmd, fh):
        self.store[cmd.split(" ", 1)[1]] = json.loads(fh.read())
        self.store["uploads"] = self.store.get("uploads", 0) + 1

    def quit(self):
        pass


def pcfg(**kw):
    base = dict(ftp=FtpConfig(host="h", username="u", password="p"), objective=4000,
                charity_url="https://charity.test/team")
    base.update(kw)
    return ProgressConfig(**base)


@pytest.fixture
def store():
    return {}


def bar(db, store, charity=None, **kw):
    return ProgressBar(pcfg(**kw), db, http=charity or FakeCharity(), ftp_factory=FakeFTP(store))


def test_same_json_as_last_year(cfg, db, notifier, people, store):
    process_message(cfg, db, notifier, make_email("Montant : CHF 20.50\nPour : Bob", msgid="<1>"))
    process_message(cfg, db, notifier, make_email("Montant : CHF 10\nPour : Bob", msgid="<2>"))
    process_message(cfg, db, notifier, make_email("pas de montant", msgid="<3>"))
    b = bar(db, store)
    b.update()
    assert store["progress_bar.json"] == {"objective": 4000, "sl_charity": 1234.5,
                                          "heros_locaux": 30.5}
    assert list(store["progress_bar.json"]) == ["objective", "sl_charity", "heros_locaux"]


def test_offset_and_no_charity(db, store):
    b = bar(db, store, offset=-5.25, charity_url="")
    assert b.compute() == {"objective": 4000, "sl_charity": 0, "heros_locaux": -5.25}


def test_uploads_only_when_changed(cfg, db, notifier, people, store):
    charity = FakeCharity()
    b = bar(db, store, charity)
    b.update()
    b.update()
    assert store["uploads"] == 1
    charity.cents = 200000
    b.update()
    assert store["uploads"] == 2 and store["progress_bar.json"]["sl_charity"] == 2000
    process_message(cfg, db, notifier, make_email("Montant : CHF 7", msgid="<x>"))
    b.update()
    assert store["uploads"] == 3 and store["progress_bar.json"]["heros_locaux"] == 7
    b.update(force=True)
    assert store["uploads"] == 4


def test_charity_down_keeps_last_value_even_after_restart(db, store):
    charity = FakeCharity()
    bar(db, store, charity).update()
    charity.fail = True
    b2 = bar(db, store, charity)                 # "restart"
    assert b2.compute()["sl_charity"] == 1234.5


def test_ftp_error_recorded_and_retried(db, store):
    ftp = FakeFTP(store, fail=True)
    b = ProgressBar(pcfg(), db, http=FakeCharity(), ftp_factory=ftp)
    b.update()
    assert "530" in b.status()["error"]
    ftp.fail = False
    b.update()                                   # same data, but never uploaded
    assert store["uploads"] == 1 and b.status()["error"] == ""


def test_directory(db, store):
    bar(db, store, ftp=FtpConfig(host="h", username="u", password="p", directory="www/gac")).update()
    assert store["cwd"] == "www/gac"


def test_trigger_wakes_loop(db, store):
    b = bar(db, store, interval_seconds=3600)
    t = b.start_thread()
    try:
        for _ in range(50):
            if store.get("uploads"):
                break
            threading.Event().wait(0.02)
        assert store["uploads"] == 1             # published at start-up
        b.cfg.offset = 1
        b.trigger()
        for _ in range(50):
            if store.get("uploads") == 2:
                break
            threading.Event().wait(0.02)
        assert store["uploads"] == 2             # without waiting an hour
    finally:
        b.stop()
        t.join(2)


# --- real FTP / FTPS servers -------------------------------------------------
pyftpdlib = pytest.importorskip("pyftpdlib")


def _self_signed(tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(1).not_valid_before(now)
            .not_valid_after(now + datetime.timedelta(days=1)).sign(key, hashes.SHA256()))
    path = tmp_path / "cert.pem"
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                       serialization.PrivateFormat.TraditionalOpenSSL,
                                       serialization.NoEncryption())
                     + cert.public_bytes(serialization.Encoding.PEM))
    return str(path)


@pytest.fixture(params=[False, True], ids=["ftp", "ftps"])
def ftp_server(request, tmp_path):
    from pyftpdlib.authorizers import DummyAuthorizer
    from pyftpdlib.handlers import FTPHandler, TLS_FTPHandler
    from pyftpdlib.servers import FTPServer

    tls = request.param
    root = tmp_path / "ftproot"
    (root / "www").mkdir(parents=True)
    auth = DummyAuthorizer()
    auth.add_user("charity", "s3cret", str(root), perm="elradfmwMT")
    base = TLS_FTPHandler if tls else FTPHandler
    handler = type("Handler", (base,), {"authorizer": auth, "auth_failed_timeout": 0.1})
    if tls:
        handler.certfile = _self_signed(tmp_path)
        handler.tls_control_required = True      # refuse unencrypted logins
        handler.tls_data_required = True
    server = FTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, kwargs={"timeout": 0.1},
                     daemon=True).start()
    yield tls, server.socket.getsockname()[1], root
    server.close_all()


def test_real_upload(db, ftp_server):
    tls, port, root = ftp_server
    ftp = FtpConfig(host="127.0.0.1", port=port, username="charity", password="s3cret",
                    tls=tls, directory="www")
    b = ProgressBar(pcfg(ftp=ftp), db, http=FakeCharity())
    b.update()
    assert json.loads((root / "www" / "progress_bar.json").read_text()) == {
        "objective": 4000, "sl_charity": 1234.5, "heros_locaux": 0}
    assert b.status()["error"] == ""


def test_real_upload_wrong_password(db, ftp_server):
    tls, port, _ = ftp_server
    ftp = FtpConfig(host="127.0.0.1", port=port, username="charity", password="nope", tls=tls)
    b = ProgressBar(pcfg(ftp=ftp), db, http=FakeCharity())
    b.update()
    assert "530" in b.status()["error"]


def test_plain_client_refused_by_ftps_only_server(db, ftp_server):
    tls, port, _ = ftp_server
    if not tls:
        pytest.skip("only meaningful for the FTPS server")
    ftp = FtpConfig(host="127.0.0.1", port=port, username="charity", password="s3cret",
                    tls=False)
    b = ProgressBar(pcfg(ftp=ftp), db, http=FakeCharity())
    b.update()
    assert b.status()["error"]


def test_check_progress_command(tmp_path, raw_cfg, capsys, monkeypatch, ftp_server):
    import yaml

    import gamersacoeur_notif.__main__ as cli
    import gamersacoeur_notif.progress as progress_mod

    tls, port, root = ftp_server
    raw_cfg["progress_bar"] = {"objective": 5000, "charity_url": "",
                               "ftp": {"host": "127.0.0.1", "port": port, "username": "charity",
                                       "password": "s3cret", "tls": tls}}
    path = tmp_path / "c.yaml"
    path.write_text(yaml.safe_dump(raw_cfg, allow_unicode=True), encoding="utf-8")

    assert cli.main(["-c", str(path), "check-progress", "--no-upload"]) == 0
    assert '"objective": 5000' in capsys.readouterr().out
    assert not (root / "progress_bar.json").exists()

    assert cli.main(["-c", str(path), "check-progress"]) == 0
    assert "OK - uploaded" in capsys.readouterr().out
    assert json.loads((root / "progress_bar.json").read_text())["objective"] == 5000


def test_admin_shows_progress(raw_cfg, db, notifier, store):
    import base64

    from gamersacoeur_notif.config import config_from_dict
    from gamersacoeur_notif.web import create_app

    raw_cfg["progress_bar"] = {"ftp": {"host": "h", "username": "u", "password": "p"}}
    cfg = config_from_dict(raw_cfg)
    b = bar(db, store)
    b.update()
    app = create_app(cfg, db, notifier, b)
    auth = {"Authorization": "Basic " + base64.b64encode(b"a:adminpw").decode()}
    page = app.test_client().get("/admin", headers=auth).get_data(as_text=True)
    assert "Barre de progression" in page and "1234.5" in page and "4000" in page
    assert "Mettre à jour maintenant" in page


def test_charity_unexpected_error_keeps_last_value(db, store):
    class Broken:
        def get(self, url, timeout):
            raise OSError("Could not find a suitable TLS CA certificate bundle")
    b = ProgressBar(pcfg(), db, http=Broken(), ftp_factory=FakeFTP(store))
    assert b.compute()["sl_charity"] == 0
