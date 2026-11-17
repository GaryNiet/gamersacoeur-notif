"""Web app: streamers connect their Streamlabs account, organisers monitor donations.

Routes
  /           the one link sent to every streamer -> "Log in with Streamlabs"
  /callback   Streamlabs OAuth redirect URI; links the account to the streamer
              whose ``streamlabs_name`` matches, otherwise leaves it unassigned
  /admin      dashboard (HTTP basic auth, admin_password)
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from functools import wraps
from urllib.parse import urlsplit

from flask import Flask, abort, flash, redirect, render_template, request, session, url_for

from . import streamlabs
from .config import Config
from .db import Database
from .notifier import Notifier


def public_base_url(cfg: Config) -> str:
    """Public URL of the app, derived from the Streamlabs redirect URI."""
    parts = urlsplit(cfg.streamlabs.redirect_uri)
    prefix = parts.path.rsplit("/callback", 1)[0] if parts.path.endswith("/callback") else ""
    return f"{parts.scheme}://{parts.netloc}{prefix}"


def connect_link(cfg: Config) -> str:
    """The single link to send to every streamer."""
    return public_base_url(cfg) + "/"


def create_app(cfg: Config, db: Database, notifier: Notifier | None = None) -> Flask:
    app = Flask(__name__)
    app.secret_key = cfg.secret_key
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")
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
            # Streamlabs didn't tell us who this is: key the account on its token
            # so the admin can still assign it by hand.
            account_id = "token:" + hashlib.sha256(tok["access_token"].encode()).hexdigest()[:16]
        display = names[0] if names else None
        st = cfg.streamer_for_account(names)
        acc_id = db.save_account(account_id, display, tok["access_token"],
                                 tok.get("refresh_token"), slug=st.slug if st else None)
        app.logger.info("Streamlabs account %s (%s) connected -> %s",
                        account_id, ", ".join(names) or "?", st.slug if st else "unassigned")

        # A known account keeps its link even if it wasn't matched by name this time.
        linked = cfg.streamer(db.get_account(acc_id)["slug"] or "")
        label = f" ({display})" if display else ""
        if linked:
            return render_template("message.html", ok=True, text=(
                f"Merci {linked.name} ! Ton compte Streamlabs{label} est connecté. "
                "Les dons TWINT apparaîtront dans ton Alert Box."))
        return render_template("message.html", ok=True, text=(
            f"Ton compte Streamlabs{label} est connecté, mais on ne sait pas encore à quel "
            "streamer il correspond. Préviens l'organisation : elle va le valider de son côté."))

    # --- admin ---------------------------------------------------------
    @app.get("/admin")
    @require_admin
    def admin():
        tokens = db.all_tokens()
        streamers = [{"cfg": st, "token": tokens.get(st.slug)} for st in cfg.streamers]
        donations = [{"row": d, "deliveries": db.deliveries_for(d["id"])}
                     for d in db.recent_donations(100)]
        return render_template(
            "admin.html", cfg=cfg, streamers=streamers, donations=donations,
            unassigned=db.unassigned_accounts(), link=connect_link(cfg),
            last_poll=db.get_state("last_poll"), worker_error=db.get_state("worker_error"))

    @app.post("/admin/assign/<int:account_id>")
    @require_admin
    def admin_assign(account_id):
        slug = request.form.get("slug", "")
        if cfg.streamer(slug) is None or db.get_account(account_id) is None:
            abort(404)
        db.assign_account(account_id, slug)
        flash(f"Compte attribué à {cfg.streamer(slug).name}", "ok")
        return redirect(url_for("admin"))

    @app.post("/admin/forget/<int:account_id>")
    @require_admin
    def admin_forget(account_id):
        db.delete_account(account_id)
        flash("Compte supprimé", "ok")
        return redirect(url_for("admin"))

    @app.post("/admin/test/<slug>")
    @require_admin
    def admin_test(slug):
        if cfg.streamer(slug) is None:
            abort(404)
        status, detail = notifier.send_test(slug)
        flash(f"Test {slug}: {status} {detail}", "ok" if status in ("sent", "dry_run") else "err")
        return redirect(url_for("admin"))

    @app.post("/admin/disconnect/<slug>")
    @require_admin
    def admin_disconnect(slug):
        acc = db.get_token(slug)
        if acc is not None:
            db.delete_account(acc["id"])
        flash(f"{slug} déconnecté", "ok")
        return redirect(url_for("admin"))

    @app.post("/admin/retry/<int:donation_id>")
    @require_admin
    def admin_retry(donation_id):
        results = notifier.retry(donation_id)
        flash(f"Don #{donation_id} renvoyé: {results or 'rien à renvoyer'}", "ok")
        return redirect(url_for("admin"))

    return app
