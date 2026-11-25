"""Read new emails from a Microsoft 365 / Outlook mailbox via Microsoft Graph.

Uses an app registration (Entra ID) with the *application* permission
Mail.Read (admin consent granted) and a client secret: no user login needed.
Messages are only read, never marked as read or moved.

The position is stored in the database as the receivedDateTime of the last
processed message (plus the ids seen at that exact second), so a restart
picks up where it left off.
"""

from __future__ import annotations

import email
import json
import logging
import time
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage
from urllib.parse import quote

import requests

from .config import MailConfig
from .db import Database

log = logging.getLogger(__name__)

GRAPH = "https://graph.microsoft.com/v1.0"
LOGIN = "https://login.microsoftonline.com"
TIMEOUT = 30
WELL_KNOWN = {"inbox", "archive", "junkemail", "deleteditems", "sentitems", "drafts"}


class GraphError(Exception):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class GraphMailbox:
    def __init__(self, cfg: MailConfig, db: Database, http=None):
        self.cfg = cfg
        self.db = db
        self.http = http or requests.Session()
        self._token: str | None = None
        self._token_expiry = 0.0
        self._folder: str | None = None
        self._state_key = f"graph:{cfg.mailbox.lower()}/{cfg.folder.lower()}"
        self._user = f"{GRAPH}/users/{quote(cfg.mailbox)}"

    # --- auth / http ---------------------------------------------------
    def _access_token(self) -> str:
        if self._token and time.time() < self._token_expiry - 60:
            return self._token
        r = self.http.post(f"{LOGIN}/{self.cfg.tenant_id}/oauth2/v2.0/token", timeout=TIMEOUT,
                           data={"grant_type": "client_credentials",
                                 "client_id": self.cfg.client_id,
                                 "client_secret": self.cfg.client_secret,
                                 "scope": "https://graph.microsoft.com/.default"})
        if r.status_code >= 400:
            raise GraphError(f"Microsoft login failed ({r.status_code}): {r.text[:300]}")
        data = r.json()
        self._token = data["access_token"]
        self._token_expiry = time.time() + int(data.get("expires_in", 3600))
        return self._token

    def _get(self, url: str, params: dict | None = None, raw: bool = False):
        for attempt in (1, 2):
            r = self.http.get(url, params=params, timeout=TIMEOUT,
                              headers={"Authorization": f"Bearer {self._access_token()}"})
            if r.status_code == 401 and attempt == 1:
                self._token = None          # expired/revoked: get a new one once
                continue
            if r.status_code >= 400:
                hint = (" - does the app have the Mail.Read application permission "
                        "with admin consent?" if r.status_code == 403 else "")
                raise GraphError(f"Graph GET failed ({r.status_code}){hint}: {r.text[:300]}")
            return r.content if raw else r.json()
        raise GraphError("unreachable")

    # --- folder / state ------------------------------------------------
    def _resolve_folder(self) -> str:
        name = self.cfg.folder.strip()
        if name.lower() in WELL_KNOWN:
            return name.lower()
        flt = {"$filter": "displayName eq '" + name.replace("'", "''") + "'"}
        for url in (f"{self._user}/mailFolders", f"{self._user}/mailFolders/inbox/childFolders"):
            found = self._get(url, dict(flt)).get("value", [])
            if found:
                return found[0]["id"]
        raise GraphError(f"Mail folder '{name}' not found (top level or under Inbox)")

    def _state(self) -> dict:
        return json.loads(self.db.get_state(self._state_key) or "{}")

    def _save_state(self, since: str, seen: list[str]) -> None:
        self.db.set_state(self._state_key, json.dumps({"since": since, "seen": seen[-200:]}))

    # --- mailbox interface (same as the IMAP Mailbox) --------------------
    def connect(self) -> None:
        self._access_token()
        self._folder = self._resolve_folder()
        if not self._state():
            since = "1970-01-01T00:00:00Z" if self.cfg.process_existing else _utc_now()
            self._save_state(since, [])
            log.info("Mailbox baseline set at %s", since)

    def close(self) -> None:
        pass

    def fetch_new(self) -> list[tuple[tuple[str, str], EmailMessage]]:
        st = self._state()
        since, seen = st["since"], set(st.get("seen", []))
        url = f"{self._user}/mailFolders/{self._folder}/messages"
        params = {"$filter": f"receivedDateTime ge {since}",
                  "$orderby": "receivedDateTime asc",
                  "$select": "id,receivedDateTime", "$top": "50"}
        out = []
        while url:
            data = self._get(url, params)
            params = None                      # nextLink already carries them
            for m in data.get("value", []):
                received, mid = m["receivedDateTime"], m["id"]
                if received < since or (received == since and mid in seen):
                    continue
                mime = self._get(f"{self._user}/messages/{mid}/$value", raw=True)
                out.append(((received, mid),
                            email.message_from_bytes(mime, policy=policy.default)))
            url = data.get("@odata.nextLink")
        return out

    def recent(self, n: int = 5) -> list[EmailMessage]:
        """The n most recent messages of the folder (newest first), for checking."""
        data = self._get(f"{self._user}/mailFolders/{self._folder}/messages",
                         {"$orderby": "receivedDateTime desc", "$select": "id", "$top": str(n)})
        return [email.message_from_bytes(self._get(f"{self._user}/messages/{m['id']}/$value",
                                                   raw=True), policy=policy.default)
                for m in data.get("value", [])]

    def mark_done(self, key: tuple[str, str]) -> None:
        received, mid = key
        st = self._state()
        if received > st["since"]:
            self._save_state(received, [mid])
        elif received == st["since"] and mid not in st.get("seen", []):
            self._save_state(received, st.get("seen", []) + [mid])
