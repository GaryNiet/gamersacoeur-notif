"""Minimal Streamlabs API v2.0 client (OAuth + donations + alerts).

API reference: https://dev.streamlabs.com/
"""

from __future__ import annotations

import re
import unicodedata
from decimal import Decimal
from urllib.parse import urlencode

import requests

API = "https://streamlabs.com/api/v2.0"
SCOPES = ("donations.create", "alerts.create")
TIMEOUT = 15


class StreamlabsError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def authorize_url(client_id: str, redirect_uri: str, state: str) -> str:
    return f"{API}/authorize?" + urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "state": state,
    })


def _token_request(data: dict) -> dict:
    r = requests.post(f"{API}/token", data=data, timeout=TIMEOUT)
    if r.status_code >= 400:
        raise StreamlabsError(f"token request failed: {r.status_code} {r.text[:300]}", r.status_code)
    return r.json()


def exchange_code(client_id, client_secret, redirect_uri, code) -> dict:
    return _token_request({
        "grant_type": "authorization_code",
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "code": code,
    })


def refresh_token(client_id, client_secret, redirect_uri, refresh) -> dict:
    return _token_request({
        "grant_type": "refresh_token",
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "refresh_token": refresh,
    })


def get_user(access_token: str) -> tuple[str | None, list[str]]:
    """Return (account id, display names) of the logged-in account.

    The names include the Streamlabs name and those of linked Twitch /
    YouTube / ... accounts, so a streamer can be matched on any of them.
    Returns (None, []) if Streamlabs doesn't answer.
    """
    try:
        r = requests.get(f"{API}/user", headers={"Authorization": f"Bearer {access_token}"},
                         timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError):
        return None, []
    account_id, names = None, []
    for key, info in data.items():
        if not isinstance(info, dict):
            continue
        if key == "streamlabs" and info.get("id") is not None:
            account_id = str(info["id"])
        for field in ("display_name", "name", "username", "title"):
            val = info.get(field)
            if isinstance(val, str) and val and val not in names:
                names.append(val)
    return account_id, names


def sanitize_donor_name(name: str, fallback: str = "Anonyme") -> str:
    """Streamlabs donor names: 2-25 chars, letters/digits/underscore/space."""
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^A-Za-z0-9_ ]+", "_", s)
    s = re.sub(r"_+", "_", s).strip(" _")[:25].strip(" _")
    return s if len(s) >= 2 else fallback


def _post(path: str, access_token: str, data: dict) -> dict:
    try:
        r = requests.post(f"{API}{path}", data=data, timeout=TIMEOUT,
                          headers={"Authorization": f"Bearer {access_token}"})
    except requests.RequestException as exc:
        raise StreamlabsError(f"network error: {exc}") from exc
    if r.status_code >= 400:
        raise StreamlabsError(f"{path} failed: {r.status_code} {r.text[:300]}", r.status_code)
    try:
        return r.json()
    except ValueError:
        return {}


def create_donation(access_token: str, *, name: str, amount: Decimal, currency: str,
                    message: str = "", identifier: str = "twint") -> dict:
    return _post("/donations", access_token, {
        "name": sanitize_donor_name(name),
        "message": message[:255],
        "identifier": identifier[:64] or "twint",
        "amount": str(amount),
        "currency": currency,
    })


def create_alert(access_token: str, *, message: str, user_message: str = "",
                 alert_type: str = "donation") -> dict:
    data = {"type": alert_type, "message": message[:255]}
    if user_message:
        data["user_message"] = user_message[:255]
    return _post("/alerts", access_token, data)
