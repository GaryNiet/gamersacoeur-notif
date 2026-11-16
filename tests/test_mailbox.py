from gamersacoeur_notif.mailbox import Mailbox

from .conftest import make_email


class FakeIMAP:
    def __init__(self, messages, validity="1"):
        self.messages = messages  # uid -> EmailMessage
        self.validity = validity

    def response(self, code):
        return code, [self.validity.encode()]

    def noop(self):
        return "OK", [b""]

    def uid(self, cmd, *args):
        if cmd == "SEARCH":
            start = int(args[1].split()[1].split(":")[0])
            uids = [u for u in sorted(self.messages) if u >= start]
            if not uids and self.messages:      # real servers return the last msg for n:*
                uids = [max(self.messages)]
            return "OK", [" ".join(map(str, uids)).encode()]
        if cmd == "FETCH":
            u = int(args[0])
            return "OK", [(b"x", self.messages[u].as_bytes()), b")"]
        raise AssertionError(cmd)


def box_with(cfg, db, imap):
    box = Mailbox(cfg.mail, db)
    box.conn = imap
    box._check_uidvalidity()
    return box


def test_existing_mail_ignored_then_new_mail_fetched(cfg, db):
    imap = FakeIMAP({1: make_email("old", msgid="<1@x>"), 2: make_email("old2", msgid="<2@x>")})
    box = box_with(cfg, db, imap)
    assert box.fetch_new() == []            # baseline = 2, last msg not returned again
    imap.messages[3] = make_email("new", msgid="<3@x>")
    got = box.fetch_new()
    assert [u for u, _ in got] == [3]
    assert got[0][1]["Message-ID"] == "<3@x>"
    box.mark_done(3)
    assert box.fetch_new() == []


def test_position_survives_restart(cfg, db):
    imap = FakeIMAP({1: make_email("a")})
    box_with(cfg, db, imap).mark_done(1)
    imap.messages[2] = make_email("b")
    assert [u for u, _ in box_with(cfg, db, imap).fetch_new()] == [2]


def test_process_existing(cfg, db):
    cfg.mail.process_existing = True
    box = box_with(cfg, db, FakeIMAP({1: make_email("a"), 2: make_email("b")}))
    assert [u for u, _ in box.fetch_new()] == [1, 2]


def test_uidvalidity_change_resets(cfg, db):
    imap = FakeIMAP({5: make_email("a")})
    box_with(cfg, db, imap)
    imap2 = FakeIMAP({1: make_email("x")}, validity="2")
    assert box_with(cfg, db, imap2).fetch_new() == []
