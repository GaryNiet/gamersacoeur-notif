"""Web app: streamers connect their Streamlabs account, organisers monitor donations.

Routes
  /           the one link sent to every streamer -> "Log in with Streamlabs"
  /callback   Streamlabs OAuth redirect URI; creates (or refreshes) the streamer,
              with their Streamlabs/Twitch/YouTube names as routing keywords
  /admin      dashboard (HTTP basic auth, admin_password)
"""

from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import secrets
from functools import wraps
from urllib.parse import urlsplit

from flask import (Flask, Response, abort, flash, redirect, render_template, request, session,
                   url_for)

from . import streamlabs
from .config import Config
from .db import Database
from .notifier import Notifier


def public_base_url(cfg: Config) -> str:
    """Public URL of the app, derived from the Streamlabs redirect URI."""
    parts = urlsplit(cfg.streamlabs.redirect_uri)
    prefix = parts.path.rsplit("/callback", 1)[0] if parts.path.endswith("/callback") else ""
    return f"{parts.scheme}://{parts.netloc}{prefix}"


def csv_cell(value) -> str:
    """Text for a CSV cell; donor-supplied text can't run as a spreadsheet formula."""
    s = "" if value is None else str(value)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def donation_fields(row) -> dict[str, str]:
    """The "Key: value" fields stored with a donation (empty for old donations)."""
    try:
        return json.loads(row["fields"] or "{}")
    except (IndexError, KeyError, ValueError):
        return {}


def connect_link(cfg: Config) -> str:
    """The single link to send to every streamer."""
    return public_base_url(cfg) + "/"


def create_app(cfg: Config, db: Database, notifier: Notifier | None = None,
               progress=None) -> Flask:
    app = Flask(__name__)
    app.secret_key = cfg.secret_key
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                      SESSION_COOKIE_SECURE=cfg.streamlabs.redirect_uri.startswith("https:"))
    notifier = notifier or Notifier(cfg, db)

    # --- helpers -------------------------------------------------------
    def require_admin(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            auth = request.authorization
            if not auth or not hmac.compare_digest(
                    (auth.password or "").encode(), cfg.admin_password.encode()):
                return ("Authentification requise", 401,
                        {"WWW-Authenticate": 'Basic realm="Gamers a Coeur admin"'})
            if request.method == "POST":
                token = request.form.get("csrf", "")
                if not token or not hmac.compare_digest(token, session.get("csrf", "")):
                    abort(400, "CSRF token invalide")
            return view(*args, **kwargs)
        return wrapper

    @app.context_processor
    def inject():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(24)
        return {"csrf": session["csrf"]}

    # --- streamer login ------------------------------------------------
    @app.get("/")
    def index():
        state = secrets.token_urlsafe(24)
        session["oauth_state"] = state
        return render_template("index.html", url=streamlabs.authorize_url(
            cfg.streamlabs.client_id, cfg.streamlabs.redirect_uri, state))

    @app.get("/callback")
    def callback():
        state = request.args.get("state", "")
        expected = session.pop("oauth_state", None)
        if not expected or not hmac.compare_digest(state, expected):
            return render_template("message.html", ok=False, text=(
                "Session expirée. Rouvre le lien et réessaie.")), 400
        if "error" in request.args or "code" not in request.args:
            return render_template("message.html", ok=False,
                                   text="Connexion Streamlabs annulée ou refusée."), 400
        sl = cfg.streamlabs
        try:
            tok = streamlabs.exchange_code(sl.client_id, sl.client_secret, sl.redirect_uri,
                                           request.args["code"])
        except streamlabs.StreamlabsError as exc:
            app.logger.error("OAuth exchange failed: %s", exc)
            return render_template("message.html", ok=False,
                                   text="Streamlabs a refusé la connexion. Réessaie."), 502

        account_id, names = streamlabs.get_user(tok["access_token"])
        if account_id is None:
            # Streamlabs didn't say who this is: key the streamer on its token;
            # the admin can add keywords by hand.
            account_id = "token:" + hashlib.sha256(tok["access_token"].encode()).hexdigest()[:16]
        display = names[0] if names else None
        sid, created = db.upsert_streamer(
            account_id, display, names, tok["access_token"], tok.get("refresh_token"),
            main=cfg.is_main_account(names), enabled=cfg.new_streamers_enabled)
        row = db.get_streamer_row(sid)
        app.logger.info("Streamer #%s %s (%s) %s", sid, row["name"], ", ".join(names) or "?",
                        "signed up" if created else "reconnected")

        if not row["enabled"]:
            return render_template("message.html", ok=True, text=(
                f"Merci {row['name']} ! Ton compte Streamlabs est enregistré. L'organisation "
                "doit encore l'activer : tu seras prévenu·e."))
        return render_template("connected.html", streamer=row, names=json.loads(row["keywords"]))

    # --- admin ---------------------------------------------------------
    @app.get("/admin")
    @require_admin
    def admin():
        rows = db.streamer_rows()
        names = {str(r["id"]): r["name"] for r in rows}
        donations = [{"row": d, "deliveries": db.deliveries_for(d["id"]),
                      "fields": donation_fields(d)}
                     for d in db.recent_donations(100)]
        return render_template(
            "admin.html", cfg=cfg, streamers=rows, names=names, donations=donations,
            link=connect_link(cfg), keywords=lambda r: ", ".join(json.loads(r["keywords"])),
            has_main=any(r["is_main"] and r["enabled"] for r in rows),
            last_poll=db.get_state("last_poll"), worker_error=db.get_state("worker_error"),
            progress=json.loads(db.get_state("progress_bar") or "{}") if cfg.progress_bar else None,
            progress_running=progress is not None)

    @app.get("/admin/donations.csv")
    @require_admin
    def admin_donations_csv():
        names = {str(r["id"]): r["name"] for r in db.streamer_rows()}
        buf = io.StringIO()
        # Semicolons + BOM: opens directly in Excel with a Swiss/French locale.
        w = csv.writer(buf, delimiter=";")
        rows = db.all_donations()
        # One extra column per "Key: value" field found in any email, in first-seen order.
        all_fields = [donation_fields(r) for r in rows]
        keys = list(dict.fromkeys(k for f in all_fields for k in f))
        w.writerow(["id", "recu_utc", "donateur", "montant", "devise", "message", "pour",
                    "statut", "envois", "sujet"] + [f"email: {k}" for k in keys]
                   + ["texte_email"])
        for r, fields in zip(rows, all_fields):
            sends = ", ".join(f"{names.get(dl['streamer_id'], '#' + str(dl['streamer_id']))}: "
                              f"{dl['status']}" for dl in db.deliveries_for(r["id"]))
            w.writerow([csv_cell(v) for v in (
                r["id"], r["received_at"], r["donor"], r["amount"], r["currency"], r["message"],
                names.get(r["routed_to"], f"#{r['routed_to']}") if r["routed_to"] else "",
                r["status"], sends, r["subject"], *(fields.get(k, "") for k in keys),
                r["email_text"])])
        return Response("﻿" + buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=dons-twint.csv"})

    @app.post("/admin/streamer/<int:streamer_id>")
    @require_admin
    def admin_update(streamer_id):
        if db.get_streamer_row(streamer_id) is None:
            abort(404)
        keywords = [k.strip() for k in request.form.get("keywords", "").split(",") if k.strip()]
        db.update_streamer(streamer_id, keywords=keywords, main="main" in request.form,
                           enabled="enabled" in request.form)
        flash("Streamer mis à jour", "ok")
        return redirect(url_for("admin"))

    @app.post("/admin/streamer/<int:streamer_id>/delete")
    @require_admin
    def admin_delete(streamer_id):
        db.delete_streamer(streamer_id)
        flash("Streamer supprimé", "ok")
        return redirect(url_for("admin"))

    @app.post("/admin/test/<int:streamer_id>")
    @require_admin
    def admin_test(streamer_id):
        row = db.get_streamer_row(streamer_id)
        if row is None:
            abort(404)
        status, detail = notifier.send_test(str(streamer_id))
        flash(f"Test {row['name']}: {status} {detail}",
              "ok" if status in ("sent", "dry_run", "queued") else "err")
        return redirect(url_for("admin"))

    @app.post("/admin/progress")
    @require_admin
    def admin_progress():
        if progress is None:
            abort(404)
        progress.trigger()
        flash("Mise à jour de la barre de progression demandée", "ok")
        return redirect(url_for("admin"))

    @app.post("/admin/retry/<int:donation_id>")
    @require_admin
    def admin_retry(donation_id):
        names = notifier.retry(donation_id)
        flash(f"Don #{donation_id} remis en file pour : {', '.join(names)}" if names
              else f"Don #{donation_id} : rien à renvoyer", "ok")
        return redirect(url_for("admin"))

    return app
