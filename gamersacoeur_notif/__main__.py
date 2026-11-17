"""Command line entry point.

    python -m gamersacoeur_notif run            # web app + mailbox worker (normal use)
    python -m gamersacoeur_notif web            # web app only
    python -m gamersacoeur_notif worker         # mailbox worker only
    python -m gamersacoeur_notif link           # print the link to send to all streamers
    python -m gamersacoeur_notif parse mail.eml # show what would be extracted from an email
"""

from __future__ import annotations

import argparse
import email
import logging
import os
import sys
from email import policy

from .config import ConfigError, load_config
from .db import Database
from .mailbox import matches_filters
from .notifier import Notifier
from .parsing import email_text, find_streamer, parse_donation
from .web import connect_link, create_app
from .worker import Worker


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
            subject = str(msg.get("Subject", ""))
            text = email_text(msg)
            d = parse_donation(subject, text, cfg.parsing)
            st = find_streamer(d.streamer_hint, streamers)
            print(f"=== {path}")
            print(f"From     : {msg.get('From')}")
            print(f"Subject  : {subject}")
            print(f"Filters  : {'match' if matches_filters(msg, cfg.mail) else 'IGNORED (from/subject filter)'}")
            print(f"Amount   : {d.amount} {d.currency}" if d.amount else "Amount   : NOT FOUND")
            print(f"Donor    : {d.donor}")
            print(f"Message  : {d.message}")
            print(f"Streamer : {st.name if st else 'no match -> main account only'}")
            print(f"Main     : {', '.join(s.name for s in streamers if s.main) or '(none yet)'}")
            print("--- body text used for parsing ---")
            print(text)
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
    if args.cmd == "worker":
        Worker(cfg, db, notifier).run_forever()
        return 0
    if args.cmd == "run":
        Worker(cfg, db, notifier).start_thread()
    app = create_app(cfg, db, notifier)
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
