import time

from gamersacoeur_notif.notifier import Notifier
from gamersacoeur_notif.pacer import Pacer
from gamersacoeur_notif.worker import process_message

from .conftest import make_email

GAP = 0.3   # stands in for the real 10 s


def emails(*targets):
    return [make_email(f"Montant : CHF {i + 1}\nNom : D{i}\nPour : {t}", msgid=f"<{i}@x>")
            for i, t in enumerate(targets)]


def gaps(api, token):
    ts = [t for tok, t in api.times if tok == token]
    return [b - a for a, b in zip(ts, ts[1:])]


def test_pacer_spaces_same_key_not_others():
    p = Pacer(GAP)
    done = []
    for key in ("a", "a", "a", "b"):
        p.submit(key, lambda key=key: done.append((key, time.monotonic())))
    p.wait_idle()
    a = [t for k, t in done if k == "a"]
    b = [t for k, t in done if k == "b"]
    assert all(y - x >= GAP * 0.95 for x, y in zip(a, a[1:]))
    assert b[0] - a[0] < GAP / 2        # another streamer isn't held up


def test_pacer_survives_failing_job():
    p = Pacer(0)
    p.submit("a", lambda: 1 / 0)
    assert p.submit("a", lambda: "ok").result(timeout=2) == "ok"


def test_burst_of_emails_is_spaced_per_account(cfg, db, api, people):
    cfg.streamlabs.min_interval_seconds = GAP
    notifier = Notifier(cfg, db, api=api)
    start = time.monotonic()
    for msg in emails("Bob", "Alice", "Bob", "personne"):
        process_message(cfg, db, notifier, msg)
    assert time.monotonic() - start < GAP     # reading emails is never blocked

    notifier.pacer.wait_idle()
    # main gets all 4, in arrival order, each >= GAP apart
    main_amounts = [c[2]["amount"] for c in api.calls if c[1] == "tok-main"]
    assert [str(a) for a in main_amounts] == ["1.00", "2.00", "3.00", "4.00"]
    assert all(g >= GAP * 0.95 for g in gaps(api, "tok-main"))
    assert len(gaps(api, "tok-main")) == 3
    # bob's two alerts are spaced too; alice's single alert went out right away
    assert all(g >= GAP * 0.95 for g in gaps(api, "tok-bob"))
    alice_t = next(t for tok, t in api.times if tok == "tok-alice")
    assert alice_t - start < GAP / 2
    assert {d["status"] for d in db.recent_donations()} == {"sent"}


def test_status_is_queued_while_waiting(cfg, db, api, people):
    cfg.streamlabs.min_interval_seconds = GAP
    notifier = Notifier(cfg, db, api=api)
    ids = [process_message(cfg, db, notifier, m) for m in emails("Bob", "Bob")]
    time.sleep(GAP / 3)
    assert db.get_donation(ids[0])["status"] == "sent"
    assert db.get_donation(ids[1])["status"] == "queued"
    assert notifier.retry(ids[1]) == []          # already queued: no duplicate
    notifier.pacer.wait_idle()
    assert db.get_donation(ids[1])["status"] == "sent"
    assert len(api.calls) == 4


def test_restart_resumes_queued_deliveries(cfg, db, api, people):
    cfg.streamlabs.min_interval_seconds = 60       # second alert never gets sent...
    notifier = Notifier(cfg, db, api=api)
    ids = [process_message(cfg, db, notifier, m) for m in emails("Bob", "Bob")]
    time.sleep(0.1)
    assert db.get_donation(ids[1])["status"] == "queued"

    # ...the app "restarts": a new notifier picks the queued work back up.
    cfg.streamlabs.min_interval_seconds = 0
    api2 = type(api)()
    fresh = Notifier(cfg, db, api=api2)
    fresh.resume_interrupted()
    fresh.pacer.wait_idle()
    assert [c[1] for c in api2.calls] == ["tok-bob", "tok-main"]
    assert db.get_donation(ids[1])["status"] == "sent"
    assert {d["status"] for d in db.deliveries_for(ids[1])} == {"interrupted", "sent"}


def test_admin_test_waits_its_turn(cfg, db, api, people):
    cfg.streamlabs.min_interval_seconds = GAP
    notifier = Notifier(cfg, db, api=api)
    process_message(cfg, db, notifier, emails("Bob")[0])
    assert notifier.send_test(people["bob"].id)[0] == "sent"
    assert gaps(api, "tok-bob")[0] >= GAP * 0.95
    assert notifier.send_test(people["bob"].id, timeout=0.01)[0] == "queued"
    notifier.pacer.wait_idle()
