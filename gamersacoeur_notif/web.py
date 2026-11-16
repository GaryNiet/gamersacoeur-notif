"""Web app: streamers connect their Streamlabs account, organisers monitor donations.

Routes
  /                          landing page
  /connect/<slug>/<sig>      personal link sent to each streamer -> Streamlabs login
  /callback                  Streamlabs OAuth redirect URI
  /admin                     dashboard (HTTP basic auth, admin_password)
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


def connect_signature(secret_key: str, slug: str) -> str:
    return hmac.new(secret_key.encode(), f"connect:{slug}".encode(),
                    hashlib.sha256).hexdigest()[:24]


def public_base_url(cfg: Config) -> str:
    """Public URL of the app, derived from the Streamlabs redirect URI."""
    parts = urlsplit(cfg.streamlabs.redirect_uri)
    prefix = parts.path.rsplit("/callback", 1)[0] if parts.path.endswith("/callback") else ""
    return f"{parts.scheme}://{parts.netloc}{prefix}"


def connect_link(cfg: Config, slug: str) -> str:
    return f"{public_base_url(cfg)}/connect/{slug}/{connect_signature(cfg.secret_key, slug)}"


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
        return render_template("index.html")

    @app.get("/connect/<slug>/<sig>")
    def connect(slug, sig):
        st = cfg.streamer(slug)
        if st is None or not hmac.compare_digest(sig, connect_signature(cfg.secret_key, slug)):
            abort(404)
        state = secrets.token_urlsafe(24)
        session["oauth_state"] = state
        session["oauth_slug"] = slug
        return render_template("connect.html", streamer=st, url=streamlabs.authorize_url(
            cfg.streamlabs.client_id, cfg.streamlabs.redirect_uri, state))

    @app.get("/callback")
    def callback():
        state = request.args.get("state", "")
        slug = session.pop("oauth_slug", None)
        expected = session.pop("oauth_state", None)
        if not slug or not expected or not hmac.compare_digest(state, expected):
            return render_template("message.html", ok=False,
                                   text="Session expirée. Rouvre ton lien personnel."), 400
        if "error" in request.args or "code" not in request.args:
            return render_template("message.html", ok=False,
                                   text="Connexion Streamlabs annulée ou refusée."), 400
        sl = cfg.streamlabs
        try:
            tok = streamlabs.exchange_code(sl.client_id, sl.client_secret, sl.redirect_uri,
                                           request.args["code"])
        except streamlabs.StreamlabsError as exc:
            app.logger.error("OAuth exchange failed for %s: %s", slug, exc)
            return render_template("message.html", ok=False,
                                   text="Streamlabs a refusé la connexion. Réessaie."), 502
        name = streamlabs.get_user_name(tok["access_token"])
        db.save_token(slug, tok["access_token"], tok.get("refresh_token"), name)
        st = cfg.streamer(slug)
        return render_template("message.html", ok=True, text=(
            f"Merci {st.name} ! Ton compte Streamlabs{f' ({name})' if name else ''} est connecté. "
            "Les dons TWINT apparaîtront dans ta Alert Box."))

    # --- admin ---------------------------------------------------------
    @app.get("/admin")
    @require_admin
    def admin():
        tokens = db.all_tokens()
        streamers = [{
            "cfg": st,
            "token": tokens.get(st.slug),
            "link": connect_link(cfg, st.slug),
        } for st in cfg.streamers]
        donations = [{"row": d, "deliveries": db.deliveries_for(d["id"])}
                     for d in db.recent_donations(100)]
        return render_template(
            "admin.html", cfg=cfg, streamers=streamers, donations=donations,
            last_poll=db.get_state("last_poll"), worker_error=db.get_state("worker_error"))

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
        db.delete_token(slug)
        flash(f"{slug} déconnecté", "ok")
        return redirect(url_for("admin"))

    @app.post("/admin/retry/<int:donation_id>")
    @require_admin
    def admin_retry(donation_id):
        results = notifier.retry(donation_id)
        flash(f"Don #{donation_id} renvoyé: {results or 'rien à renvoyer'}", "ok")
        return redirect(url_for("admin"))

    return app
