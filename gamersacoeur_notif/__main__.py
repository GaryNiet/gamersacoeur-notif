"""Command line entry point.

    python -m gamersacoeur_notif run            # web app + mailbox worker (normal use)
    python -m gamersacoeur_notif web            # web app only
    python -m gamersacoeur_notif worker         # mailbox worker only
    python -m gamersacoeur_notif link           # print the link to send to all streamers
    python -m gamersacoeur_notif parse mail.eml # show what would be extracted from an email
    python -m gamersacoeur_notif check-mail     # test the mailbox connection (sends nothing)
    python -m gamersacoeur_notif check-progress # compute + upload the progress bar once
"""

from __future__ import annotations

import argparse
import email
import json
import logging
import os
import sys
from email import policy

from .config import ConfigError, load_config
from .db import Database
from .mailbox import matches_filters, open_mailbox
from .notifier import Notifier
from .progress import ProgressBar
from .parsing import email_text, find_streamer, parse_donation
from .web import connect_link, create_app
from .worker import Worker


def describe(cfg, msg, streamers, show_text: bool) -> None:
    subject = str(msg.get("Subject", ""))
    text = email_text(msg)
    d = parse_donation(subject, text, cfg.parsing)
    st = find_streamer(d.streamer_hint, streamers)
    print(f"From     : {msg.get('From')}")
    print(f"Subject  : {subject}")
    if not matches_filters(msg, cfg.mail):
        print("Filters  : IGNORED (does not match from/subject/body filters)")
        if not show_text:
            return
    else:
        print("Filters  : match -> treated as a donation")
    print(f"Amount   : {d.amount} {d.currency}" if d.amount else "Amount   : NOT FOUND")
    print(f"Donor    : {d.donor}")
    print(f"Message  : {d.message}")
    print(f"For      : {d.streamer_hint if cfg.parsing.streamer else '(whole email)'}")
    print(f"Streamer : {st.name if st else 'no match -> main account only'}")
    if show_text:
        print(f"Main     : {', '.join(s.name for s in streamers if s.main) or '(none yet)'}")
        print("--- body text used for parsing ---")
        print(text)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="gamersacoeur_notif", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-c", "--config", default=os.environ.get("GAC_CONFIG", "config.yaml"))
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    sub.add_parser("web")
    sub.add_parser("worker")
    sub.add_parser("link")
    cp = sub.add_parser("check-progress")
    cp.add_argument("--no-upload", action="store_true", help="only print the values")
    cm = sub.add_parser("check-mail")
    cm.add_argument("-n", type=int, default=5, help="how many recent emails to show")
    p = sub.add_parser("parse")
    p.add_argument("eml", nargs="+", help=".eml file(s) (in Gmail: ⋮ > Download message)")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    if args.cmd == "parse":
        streamers = Database(cfg.database).streamers()
        for path in args.eml:
            with open(path, "rb") as fh:
                msg = email.message_from_binary_file(fh, policy=policy.default)
            print(f"=== {path}")
            describe(cfg, msg, streamers, show_text=True)
        return 0

    if args.cmd == "check-mail":
        db = Database(cfg.database)
        box = open_mailbox(cfg.mail, db)
        print(f"Connecting to {cfg.mail.description} ...")
        try:
            box.connect()
        except Exception as exc:  # noqa: BLE001 - show the reason plainly
            print(f"FAILED: {exc}", file=sys.stderr)
            return 1
        try:
            msgs = box.recent(args.n)
        finally:
            box.close()
        print(f"OK - connected. The {len(msgs)} most recent emails (nothing is sent):")
        for msg in msgs:
            print()
            describe(cfg, msg, db.streamers(), show_text=False)
        return 0

    if args.cmd == "check-progress":
        if not cfg.progress_bar:
            print("No 'progress_bar' section in the config.", file=sys.stderr)
            return 2
        pb = ProgressBar(cfg.progress_bar, Database(cfg.database))
        data = pb.compute()
        print(f"{cfg.progress_bar.filename}: {json.dumps(data)}")
        if args.no_upload:
            return 0
        f = cfg.progress_bar.ftp
        print(f"Uploading to {'ftps' if f.tls else 'ftp'}://{f.host}/{f.directory} ...")
        try:
            pb.upload(data)
        except Exception as exc:  # noqa: BLE001 - show the reason plainly
            print(f"FAILED: {exc}", file=sys.stderr)
            return 1
        print("OK - uploaded.")
        return 0

    if args.cmd == "link":
        print(connect_link(cfg))
        return 0

    db = Database(cfg.database)
    # One notifier shared by the mailbox worker and the web app, so that
    # admin resends and tests go through the same per-streamer queues.
    notifier = Notifier(cfg, db)
    if args.cmd in ("run", "worker"):
        notifier.resume_interrupted()
    progress = None
    if cfg.progress_bar and cfg.progress_bar.enabled and args.cmd in ("run", "worker"):
        progress = ProgressBar(cfg.progress_bar, db)
        progress.start_thread()
    worker = Worker(cfg, db, notifier, on_donation=progress.trigger if progress else None)
    if args.cmd == "worker":
        worker.run_forever()
        return 0
    if args.cmd == "run":
        worker.start_thread()
    app = create_app(cfg, db, notifier, progress)
    try:
        from waitress import serve
    except ImportError:  # fall back to Flask's development server
        app.run(host=cfg.host, port=cfg.port, threaded=True)
    else:
        logging.getLogger(__name__).info("Web app on http://%s:%s", cfg.host, cfg.port)
        serve(app, host=cfg.host, port=cfg.port, threads=8)
    return 0


if __name__ == "__main__":
    sys.exit(main())
